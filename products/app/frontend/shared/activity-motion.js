/* Visuals follow business events. Elapsed time never invents task progress. */
export const ACTIVITY_VISUALS = Object.freeze({
  idle: {orb: 'breathing', bloub: 'idle', label: '待命'},
  listening: {orb: 'listening', bloub: 'wide', label: '正在接收输入'},
  connecting: {orb: 'connecting', bloub: 'egg', label: '正在连接'},
  searching: {orb: 'searching', bloub: 'orbit', label: '正在检索'},
  thinking: {orb: 'solving', bloub: 'thinking', label: '正在分析'},
  calculating: {orb: 'working', bloub: 'hexagon', label: '正在计算'},
  selecting: {orb: 'shaping', bloub: 'play', label: '正在筛选结构'},
  collaborating: {orb: 'weaving', bloub: 'swirl', label: '正在整合结果'},
  composing: {orb: 'composing', bloub: 'comet', label: '正在生成答复'},
  waiting: {orb: 'breathing', bloub: 'sleep', label: '正在等待'},
  needs_input: {orb: 'listening', bloub: 'notify', label: '等待补充信息'},
  recovering: {orb: 'connecting', bloub: 'alert', label: '正在恢复连接'},
  completed: {orb: 'shaping', bloub: 'burst', label: '已完成'},
  failed: {orb: 'breathing', bloub: 'exclaim', label: '处理失败'},
  cancelled: {orb: 'breathing', bloub: 'idle', label: '已停止'},
});

export function activityForEvent(event = {}) {
  const status = event.status || '';
  const family = event.family || '';
  const type = event.type || '';
  const detail = `${event.tool || event.toolName || ''} ${event.role || ''} ${event.title || ''} ${event.summary || ''}`.toLowerCase();
  if (['needs_input','pending_approval'].includes(status)) return 'needs_input';
  if (['cancelled','stopped','interrupted'].includes(status)) return 'cancelled';
  if (['failed','timed_out','timeout'].includes(status)) return 'failed';
  if (['outcome_unknown','recovered'].includes(status) || family === 'recovery') return 'recovering';
  // A completed child/tool is not completion of the whole request.
  if (['completed','succeeded'].includes(status) && (family === 'workflow' || type === 'terminal')) return 'completed';
  if (['queued','pending','waiting_parent'].includes(status)) return 'waiting';
  if (type === 'request') return 'listening';
  if (family === 'reasoning') return 'thinking';
  if (family === 'assistant' || type === 'answer') return 'composing';
  if (family === 'compaction' || type === 'candidate_cycle') return 'collaborating';
  if (/reporter|designer|生成报告|交付材料/.test(detail)) return 'composing';
  if (/payoffer|pricer|backtest|收益结构|估值|回测|计算/.test(detail)) return 'calculating';
  if (/datafetcher.*status|检查数据|连接/.test(detail)) return 'connecting';
  if (/datafetcher|knowledger|attachment|获取|检索|读取|查询/.test(detail)) return 'searching';
  if (/selector|generator|ranker|筛选|排序|生成候选/.test(detail)) return 'selecting';
  if (/moderator|汇总|整合|并行/.test(detail)) return 'collaborating';
  if (type === 'routing' || family === 'workflow') return 'searching';
  if (family === 'tool' || type === 'host_module') return 'connecting';
  if (['starting','waiting_tool'].includes(status)) return 'connecting';
  return 'thinking';
}

// Long quiet rests between occasional gestures, with recency exclusion.
// No counter/playlist: two idle visits need not look or last the same.
export function nextIdleGesture({current = 'idle', recent = [], quietSeconds = 0, random = Math.random} = {}) {
  const options = current !== 'idle' ? [['idle', 1]] : [
    ['idle', 5], ['egg', 2], ['hexagon', 1], ['play', 1], ['wide', 2], ['wink', 2],
    ...(quietSeconds > 75 ? [['sleep', 3]] : []),
  ];
  const available = options.filter(([name]) => name === 'idle' || !recent.slice(-3).includes(name));
  let value = random() * available.reduce((total, [,weight]) => total + weight, 0);
  const name = (available.find(([,weight]) => (value -= weight) < 0) || available.at(-1))[0];
  const seconds = name === 'idle' ? 8 + random() * 15 : name === 'sleep' ? 9 + random() * 12 : 1.8 + random() * 2.8;
  return {name, seconds};
}
