from typing import List, Union, Dict, Any, Tuple

import torch

from vllm.sampling_params import SamplingParams
from vllm.sequence import SequenceGroupMetadata, SequenceGroup, SequenceStatus, Sequence
from vllm.core.scheduler import Scheduler, SchedulerOutputs
from vllm.utils import Counter
from vllm.core.block_manager import BlockSpaceManager

from .comms import AiciRunner, BenchTimer


def install(runner: AiciRunner):
    timer = BenchTimer("initiate_step")

    def initiate_step(
        scheduler: Scheduler,
        counter: Counter,
        scheduler_outputs: SchedulerOutputs,
    ):
        with timer:
            return do_initiate_step(scheduler, counter, scheduler_outputs)

    def do_initiate_step(
        scheduler: Scheduler,
        counter: Counter,
        scheduler_outputs: SchedulerOutputs,
    ):
        runner.flush_logit_bias()

        for f in scheduler.freed_seq_ids:
            runner.step_free_seq(f)

        max_context_len = 0
        num_gen = 0

        steps: list[tuple[SequenceGroup, Sequence]] = []
        for seq_group in scheduler_outputs.scheduled_seq_groups:
            seqs = seq_group.get_seqs(status=SequenceStatus.RUNNING)
            ff_seqs = [seq for seq in seqs if seq.data.num_pending_ff_tokens > 0]
            is_ff = len(ff_seqs) > 0
            if is_ff:
                # print("FF", [(seq.seq_id, seq.data.num_pending_ff_tokens, seq.skip_round) for seq in seqs])
                assert scheduler_outputs.prompt_run
                seqs = ff_seqs
            elif scheduler_outputs.prompt_run:
                assert len(seqs) == 1
            for seq in seqs:
                steps.append((seq_group, seq))
                id = seq.seq_id
                max_context_len = max(max_context_len, seq.get_len())

                if seq.skip_round:
                    seq.skip_round = False
                    num_gen += 1
                    runner.step_add_pre(id)
                elif seq.data.num_pending_ff_tokens:
                    runner.step_add_pre(id)
                elif scheduler_outputs.prompt_run:
                    runner.step_add_pre(id, req_id=seq_group.request_id)
                else:
                    num_gen += 1
                    runner.step_add_pre(id)

        if num_gen == 0:
            runner.disable_attn_mask = True

        fork_map, suspend_ids = runner.step_finish_pre(max_context_len)
        if fork_map is None:
            return
        used = [False for _ in steps]

        for _op_idx, parent_idx in enumerate(fork_map):
            seq_group, seq = steps[parent_idx]
            clone_id = None
            if used[parent_idx]:
                assert not seq.is_finished()
                copy = seq.fork(next(counter))
                seq_group.add(copy)
                seq_group.sampling_params.dynamic_forks = True
                scheduler.fork_seq(seq, copy)
                clone_id = seq.seq_id
                seq = copy
            else:
                used[parent_idx] = True
            runner.step_add_mid(seq.seq_id, clone_id=clone_id)

        for id in suspend_ids:
            seq_group, seq = steps[id]
            assert not used[id]
            # print("SUSP", seq.seq_id)
            used[id] = True
            seq.skip_round = True

        runner.step_finish_mid()

        for idx in range(len(steps)):
            if used[idx]:
                continue
            seq_group, seq = steps[idx]
            seq.status = SequenceStatus.FINISHED_ABORTED
            seq_group.remove(seq.seq_id)
            scheduler.free_seq(seq)

    def apply_dynamic_logit_bias(logits: torch.Tensor):
        bias = (
            torch.from_numpy(runner.recv_logit_bias())
            .to(logits.device)
            .to(logits.dtype)
        )
        logits += bias

    def recv_attention_mask():
        return torch.from_numpy(runner.recv_attention_mask())

    def append_ff_tokens(
        block_manager: BlockSpaceManager,
        _seq_group: SequenceGroup,
        child_seqs: List[Tuple[Sequence, Sequence]],
    ):
        for seq, parent in child_seqs:
            assert not seq.skip_round
            # lookup by parent - the child wasn't born yet when response was generated
            resp = runner.response_by_seq_id(parent.seq_id)
            backtrack: int = resp.get("backtrack", 0)
            ff: List[int] = resp.get("ff_tokens", None)
            if backtrack:
                assert seq is parent
                seq.backtrack(backtrack)
                block_manager.trim_physical_blocks(seq)
                toks = []
            else:
                toks = [seq.data.output_token_ids[-1]]
            if ff:
                # print("FF", seq.seq_id, ff, resp)
                seq.pending_ff_tokens = ff.copy()
                toks += ff
            clone_id = None
            if parent is not seq:
                clone_id = parent.seq_id
            runner.step_add_post(seq.seq_id, backtrack, toks, clone_id)

    def finish_sampling():
        runner.step_finish_post()

    SamplingParams.apply_dynamic_logit_bias = apply_dynamic_logit_bias
    SamplingParams.initiate_step = initiate_step
    SamplingParams.finish_sampling = finish_sampling
    SamplingParams.append_ff_tokens = append_ff_tokens
    SamplingParams.recv_attention_mask = recv_attention_mask