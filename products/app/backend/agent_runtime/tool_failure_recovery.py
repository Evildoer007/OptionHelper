"""Bound unsuccessful retries within one user turn without limiting valid work.

The host remembers rejected inputs, not computation results. A later user turn
always starts fresh, so repaired configuration and newly supplied inputs can run.
"""
from __future__ import annotations

from collections import Counter
import json


class ToolFailureRecovery:
    def __init__(self):
        self.failures = {}
        self.by_reason = Counter()
        self.total = 0
        self.stopped = False
        self.last_message = ''
        self.last_next_step = ''

    @staticmethod
    def key(tool, arguments):
        return tool, json.dumps(arguments, sort_keys=True, ensure_ascii=False, separators=(',', ':'), default=str)

    def before(self, tool, arguments):
        if self.stopped:
            return self.stop_result()
        prior = self.failures.get(self.key(tool, arguments))
        if prior is None:
            return None
        # Known invalid arguments cannot improve without a change. Transient
        # failures are allowed one real retry; never endlessly repeat a call.
        if prior['stable'] or prior['count'] >= 2:
            self.record(tool, arguments, prior['result'], stable=prior['stable'])
            return {**prior['result'], 'next_step': '相同请求已失败且输入未变化，未重复执行。请针对原因修正字段；不要只改标题或绕行其他计算。'}
        return None

    def record(self, tool, arguments, result, *, stable=False):
        status = str(result.get('status', '')).casefold()
        if status not in {'failed', 'error', 'unavailable', 'blocked'}:
            if status in {'completed', 'succeeded', 'success', 'ready'}:
                # A successful retry resolves this tool's prior failures.
                self.failures = {key: value for key, value in self.failures.items() if key[0] != tool}
                self.by_reason = Counter({key: value for key, value in self.by_reason.items() if key[0] != tool})
                self.total = 0
            return
        key = self.key(tool, arguments)
        prior = self.failures.get(key, {})
        self.failures[key] = {'count': prior.get('count', 0) + 1,
                              'stable': stable or prior.get('stable', False), 'result': dict(result)}
        reason = (str(result.get('failure_code') or status), str(result.get('message') or ''))
        self.by_reason[(tool, reason)] += 1
        self.total += 1
        self.last_message = str(result.get('message') or '请求未完成。')
        self.last_next_step = str(result.get('next_step') or '修正上述原因后，可以在当前任务继续。')
        # Changed inputs may still produce the same deterministic failure.
        # Three such failures stop that stalled turn; successful long workflows
        # have no new step or computation budget imposed by this guard.
        if self.by_reason[(tool, reason)] >= 3 or self.total >= 6:
            self.stopped = True

    def stop_result(self):
        return {'status': 'blocked', 'failure_code': 'repeated_tool_failure',
                'message': self.last_message, 'next_step': '本轮重复失败已停止。保留已有结果，修正原因后可继续。'}

    def summary(self):
        return ('本轮因重复失败已停止，尚未完成全部请求。\n\n原因：' + self.last_message
                + '\n\n已完成的计算和报告仍保留。' + self.last_next_step)
