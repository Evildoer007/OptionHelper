// Parameter contracts mirror Blueprint/OptionHelper参数与实施蓝图.html for the standalone Desk prototype.
const field = (label, symbol, control = 'input') => ({ label, symbol, control });

export const parameterFields = {
  U: field('标的', '𝒰'), S0: field('初始参考价', 'S₀'), S0Vec: field('各标的初始参考价', 'S_i,0'),
  T: field('合同期限', 'T'), K: field('执行价', 'K'), K1: field('低执行价', 'K₁'), K2: field('高执行价', 'K₂'),
  K3: field('第三执行价', 'K₃'), K4: field('第四执行价', 'K₄'), Kp: field('看跌执行价', 'Kₚ'),
  Kc: field('看涨执行价', 'K_c'), Ku: field('上行执行价', 'K_u'), Kd: field('下行执行价', 'K_d'),
  N: field('名义本金', 'N'), g: field('保证金比例', 'g'), Pi: field('期初期权费', 'Π₀'), pi: field('期权费率', 'π₀'),
  nC: field('看涨腿数量', 'n_call'), nP: field('看跌腿数量', 'n_put'), nj: field('期权腿数量', 'n_j'), nA: field('资产交割数量', 'n_asset'),
  Hki: field('敲入障碍', 'H_KI'), Hko: field('敲出障碍', 'H_KO'), Hkoj: field('第j期敲出障碍', 'H_KO,j'),
  Hkij: field('第j期敲入障碍', 'H_KI,j'), Hfinal: field('最终敲出障碍', 'H_KO,final'), Hc: field('派息障碍', 'H_C'),
  Htouch: field('触碰障碍', 'H_touch'), Hreset: field('重置障碍', 'H_reset'), Hhedge: field('避险线', 'H_hedge,j'),
  B: field('气囊敲入价', 'B'), Bbuf: field('缓冲价', 'B_buf'), F: field('保底比例', 'F'), Cmax: field('收益上限', 'C_max'),
  Lmax: field('限损比例', 'L_max'), alpha: field('参与率', 'α'), alphau: field('上行参与率', 'α_u'), alphad: field('下行参与率', 'α_d'),
  y: field('年化票息', 'y'), yj: field('第j期票息', 'y_j'), yko: field('敲出票息', 'y_KO'), ymat: field('到期票息', 'y_mat'),
  c: field('单期票息', 'c'), cg: field('保底票息', 'c_g'), ce: field('增强票息', 'c_e'), Q: field('固定现金支付', 'Q'),
  eta: field('固定触发收益', 'η_trig'), etako: field('敲出补偿', 'η_KO'), etau: field('上行敲出补偿', 'η_u'), etad: field('下行敲出补偿', 'η_d'),
  Oko: field('敲出观察日集', '𝒪_KO', 'textarea'), Oki: field('敲入观察日集', '𝒪_KI', 'textarea'),
  Oc: field('派息观察日集', '𝒪_C', 'textarea'), Otouch: field('触碰观察日集', '𝒪_touch', 'textarea'),
  Oreset: field('重置观察日集', '𝒪_reset', 'textarea'), Ohedge: field('避险观察日集', '𝒪_hedge', 'textarea'),
  Orange: field('区间观察日集', '𝒪_range', 'textarea'), Ovar: field('方差观察日集', '𝒪_var', 'textarea'),
  Llock: field('锁定期', 'L_lock'), n0: field('基础数量', 'n₀'), m: field('累计倍数', 'm'),
  Ksig: field('执行波动率', 'K_σ'), Nvar: field('方差名义', 'N_var'), Nvega: field('Vega名义', 'N_ν'), A: field('年化日数', 'A'),
  Hlow: field('区间下界', 'H_low'), Hup: field('区间上界', 'H_up'), Rmax: field('满额票息', 'R_max'),
  chi: field('结算规则', 'χ', 'textarea'), chiki: field('敲入结算规则', 'χ_KI', 'textarea'),
  chiko: field('敲出结算规则', 'χ_KO', 'textarea'), chipi: field('期初成本来源', 'χ_Π'),
  tv: field('估值日', 't_v', 'date'), Stv: field('估值日现价', 'S_t_v'), StvVec: field('各标的估值日现价', 'S_i,t_v'),
  tau: field('剩余期限', 'τ'), sigma: field('定价波动率', 'σ'), sigmaVec: field('各标的定价波动率', 'σ_i'),
  r: field('无风险利率', 'r'), q: field('分红率', 'q'), qVec: field('各标的分红率', 'q_i'),
  M: field('模拟路径数', 'M'), xi: field('随机种子', 'ξ'), eps: field('Greeks扰动', 'ε_S,ε_σ,ε_t,ε_r', 'textarea'),
  rho: field('相关性矩阵', 'ρ', 'textarea'), hist: field('历史价格序列', '{(t,S_t^adj)}', 'textarea'),
  histMulti: field('多标的历史价格序列', '{(t,S_i,t^adj)}_i=1^m', 'textarea'), ts: field('回测起止日', 't_start,t_end'),
  E: field('入场日集', '𝓔', 'textarea'), full: field('完整期限要求', '𝟙_full', 'select'),
  muobs: field('日频观察口径', 'μ_obs', 'select'), mumiss: field('缺失数据规则', 'μ_miss', 'select'),
  Dret: field('回测收益率分母', 'D_ret', 'select'), Vhat: field('剩余预期方差', 'V̂(t_v,T)'),
  logret: field('对数收益定义', 'ln(S_t/S_t−1)', 'textarea'), align: field('多标的对齐规则', 'μ_align', 'textarea'),
  priority: field('双边触及优先级', 'χ_priority', 'textarea'), P: field('最差表现规则', 'P_t', 'textarea'),
};

const standardPricing = ['tv', 'Stv', 'tau', 'sigma', 'r', 'q', 'M', 'xi', 'eps'];
const multiPricing = ['tv', 'StvVec', 'tau', 'sigmaVec', 'r', 'qVec', 'rho', 'M', 'xi', 'eps'];
const standardBacktest = ['hist', 'ts', 'E', 'full', 'muobs', 'mumiss', 'chipi', 'Dret'];
const multiBacktest = ['histMulti', 'ts', 'E', 'full', 'muobs', 'mumiss', 'align', 'chipi', 'Dret'];
const variancePricing = ['tv', 'hist', 'logret', 'A', 'Vhat', 'r', 'M', 'xi', 'eps'];
const varianceBacktest = ['hist', 'logret', 'ts', 'E', 'full', 'muobs', 'mumiss', 'chipi', 'Dret'];

const contract = (id, name, payoff, kind = 'single') => ({ id, name, payoff, kind });
const snowball = ['U', 'S0', 'T', 'N', 'g', 'K', 'Hko', 'Hki', 'y', 'Oko', 'Oki', 'Llock', 'chiko', 'chiki', 'chi'];

export const productContracts = [
  contract('2.1', '看涨期权', ['U', 'S0', 'T', 'K', 'nC', 'Pi', 'chi']),
  contract('2.2', '看跌期权', ['U', 'S0', 'T', 'K', 'nP', 'Pi', 'chi']),
  contract('3.1', '牛市看涨价差', ['U', 'S0', 'T', 'K1', 'K2', 'nj', 'Pi', 'chi']),
  contract('3.2', '牛市看跌价差', ['U', 'S0', 'T', 'K1', 'K2', 'nj', 'Pi', 'chi']),
  contract('3.3', '熊市看跌价差', ['U', 'S0', 'T', 'K1', 'K2', 'nj', 'Pi', 'chi']),
  contract('3.4', '熊市看涨价差', ['U', 'S0', 'T', 'K1', 'K2', 'nj', 'Pi', 'chi']),
  contract('4.1', '跨式', ['U', 'S0', 'T', 'K', 'nC', 'nP', 'Pi', 'chi']),
  contract('4.2', '宽跨式', ['U', 'S0', 'T', 'Kp', 'Kc', 'nC', 'nP', 'Pi', 'chi']),
  contract('4.3', '蝶式', ['U', 'S0', 'T', 'K1', 'K2', 'K3', 'nj', 'Pi', 'chi']),
  contract('4.4', '鹰式', ['U', 'S0', 'T', 'K1', 'K2', 'K3', 'K4', 'nj', 'Pi', 'chi']),
  contract('5.1', '向上敲出看涨', ['U', 'S0', 'T', 'K', 'Hko', 'Oko', 'nC', 'Pi', 'chiko']),
  contract('5.2', '向下敲出看涨', ['U', 'S0', 'T', 'K', 'Hko', 'Oko', 'nC', 'Pi', 'chiko']),
  contract('5.3', '向上敲入看涨', ['U', 'S0', 'T', 'K', 'Hki', 'Oki', 'nC', 'Pi', 'chiki']),
  contract('5.4', '向下敲入看涨', ['U', 'S0', 'T', 'K', 'Hki', 'Oki', 'nC', 'Pi', 'chiki']),
  contract('5.5', '向上敲出看跌', ['U', 'S0', 'T', 'K', 'Hko', 'Oko', 'nP', 'Pi', 'chiko']),
  contract('5.6', '向下敲出看跌', ['U', 'S0', 'T', 'K', 'Hko', 'Oko', 'nP', 'Pi', 'chiko']),
  contract('5.7', '向上敲入看跌', ['U', 'S0', 'T', 'K', 'Hki', 'Oki', 'nP', 'Pi', 'chiki']),
  contract('5.8', '向下敲入看跌', ['U', 'S0', 'T', 'K', 'Hki', 'Oki', 'nP', 'Pi', 'chiki']),
  contract('6.1', '现金二元看涨', ['U', 'S0', 'T', 'K', 'Q', 'Pi', 'chi']),
  contract('6.2', '现金二元看跌', ['U', 'S0', 'T', 'K', 'Q', 'Pi', 'chi']),
  contract('6.3', '资产二元看涨', ['U', 'S0', 'T', 'K', 'nA', 'Pi', 'chi']),
  contract('6.4', '资产二元看跌', ['U', 'S0', 'T', 'K', 'nA', 'Pi', 'chi']),
  contract('6.5', '触碰即付', ['U', 'S0', 'T', 'Htouch', 'Otouch', 'Q', 'Pi', 'chi']),
  contract('6.6', '不碰才付', ['U', 'S0', 'T', 'Htouch', 'Otouch', 'Q', 'Pi', 'chi']),
  contract('7.1', '标准安全气囊Airbag', ['U', 'S0', 'T', 'B', 'Oki', 'Pi', 'chiki', 'chi']),
  contract('7.2', '封顶安全气囊Airbag', ['U', 'S0', 'T', 'B', 'Oki', 'Cmax', 'Pi', 'chiki', 'chi']),
  contract('7.3', '参与率安全气囊Airbag', ['U', 'S0', 'T', 'B', 'Oki', 'alpha', 'Pi', 'chiki', 'chi']),
  contract('8.1', '标准累购', ['U', 'S0', 'T', 'K', 'Hko', 'Oko', 'n0', 'm', 'Llock', 'Pi', 'chiko', 'chi']),
  contract('9.1', '经典型雪球', snowball), contract('9.2', 'OTM型雪球', snowball),
  contract('9.3', '折价OTM雪球', snowball), contract('9.4', '折价建仓型雪球', snowball),
  contract('9.5', '超低敲入型雪球', snowball), contract('9.6', '超低敲入保底型雪球', [...snowball, 'F']),
  contract('9.7', '敲入变化型雪球', ['U', 'S0', 'T', 'N', 'g', 'K', 'Hko', 'Hkij', 'y', 'Oko', 'Oki', 'Llock', 'chiko', 'chiki', 'chi']),
  contract('9.8', '高概率敲出雪球', snowball),
  contract('9.9', '敲出递降型雪球', ['U', 'S0', 'T', 'N', 'g', 'K', 'Hkoj', 'Hki', 'y', 'Oko', 'Oki', 'Llock', 'chiko', 'chiki', 'chi']),
  contract('9.10', '降落伞雪球', ['U', 'S0', 'T', 'N', 'g', 'K', 'Hko', 'Hfinal', 'Hki', 'y', 'Oko', 'Oki', 'Llock', 'chiko', 'chiki', 'chi']),
  contract('9.11', '增强型雪球', [...snowball, 'alpha']),
  contract('9.12', '票息前置型雪球', ['U', 'S0', 'T', 'N', 'g', 'K', 'Hko', 'Hki', 'yj', 'Oko', 'Oki', 'Llock', 'chiko', 'chiki', 'chi']),
  contract('9.13', '高保底雪球', ['U', 'S0', 'T', 'N', 'g', 'Hko', 'Hki', 'y', 'F', 'Oko', 'Oki', 'Llock', 'chiko', 'chiki', 'chi']),
  contract('9.14', '救生艇雪球', ['U', 'S0', 'T', 'N', 'g', 'K', 'Hko', 'Hki', 'Hreset', 'Oko', 'Oki', 'Oreset', 'y', 'Llock', 'chiko', 'chiki', 'chi']),
  contract('9.15', '壁虎型雪球', ['U', 'S0', 'T', 'N', 'g', 'K', 'Hko', 'Hki', 'Hhedge', 'Oko', 'Oki', 'Ohedge', 'y', 'Llock', 'chiko', 'chiki', 'chi']),
  contract('9.16', '期权费型小雪球', ['U', 'S0', 'T', 'N', 'g', 'Hko', 'yko', 'ymat', 'pi', 'Oko', 'Llock', 'chiko', 'chi']),
  contract('9.17', '全保证金型小雪球', ['U', 'S0', 'T', 'N', 'Hko', 'yko', 'ymat', 'Oko', 'Llock', 'chiko', 'chi']),
  contract('9.18', 'Worst-Of雪球', ['U', 'S0Vec', 'T', 'N', 'g', 'Hko', 'Hki', 'y', 'Oko', 'Oki', 'Llock', 'P', 'chiko', 'chiki', 'chi'], 'multi'),
  contract('9.19', '凤凰式结构', ['U', 'S0', 'T', 'N', 'K', 'Hc', 'Hko', 'Hki', 'c', 'Oc', 'Oko', 'Oki', 'chiko', 'chiki', 'chi']),
  contract('9.20', '保底凤凰式结构', ['U', 'S0', 'T', 'N', 'K', 'Hc', 'Hko', 'Hki', 'c', 'F', 'Oc', 'Oko', 'Oki', 'chiko', 'chiki', 'chi']),
  contract('9.21', 'FCN无敲入', ['U', 'S0', 'T', 'N', 'K', 'Hko', 'c', 'Oc', 'Oko', 'chiko', 'chi']),
  contract('9.22', 'FCN有敲入', ['U', 'S0', 'T', 'N', 'K', 'Hko', 'Hki', 'c', 'Oc', 'Oko', 'Oki', 'chiko', 'chiki', 'chi']),
  contract('9.23', '保底FCN', ['U', 'S0', 'T', 'N', 'K', 'Hko', 'Hki', 'c', 'F', 'Oc', 'Oko', 'Oki', 'chiko', 'chiki', 'chi']),
  contract('9.24', 'DCN', ['U', 'S0', 'T', 'N', 'Hc', 'Hkoj', 'c', 'Oc', 'Oko', 'chiko', 'chi']),
  contract('9.25', '保底DCN', ['U', 'S0', 'T', 'N', 'Hc', 'Hkoj', 'cg', 'ce', 'Oc', 'Oko', 'chiko', 'chi']),
  contract('9.26', '触发器', ['U', 'S0', 'T', 'N', 'K', 'Hko', 'Hki', 'eta', 'ymat', 'Oko', 'Oki', 'chiko', 'chiki', 'chi']),
  contract('9.27', '锁盈缓冲结构', ['U', 'S0', 'T', 'N', 'g', 'Hko', 'yko', 'Bbuf', 'Oko', 'chiko', 'chi']),
  contract('9.28', '限损票息结构', ['U', 'S0', 'T', 'N', 'g', 'Hko', 'yko', 'Lmax', 'Oko', 'Llock', 'chiko', 'chi']),
  contract('9.29', '助推器', ['U', 'S0', 'T', 'N', 'g', 'K', 'Hko', 'eta', 'alpha', 'Lmax', 'Oko', 'chiko', 'chi']),
  contract('10.1', 'Worst-Of香草看涨', ['U', 'S0Vec', 'T', 'K', 'N', 'Pi', 'P', 'chi'], 'multi'),
  contract('10.2', '折价看涨8080保证金', ['U', 'S0', 'T', 'N', 'g', 'K1', 'K2', 'alpha', 'chi']),
  contract('10.3', '折价看涨8080期权费', ['U', 'S0', 'T', 'N', 'K1', 'K2', 'alpha', 'Pi', 'chi']),
  contract('10.4', '方差互换', ['U', 'T', 'Ksig', 'Nvar', 'Nvega', 'A', 'Ovar', 'Pi', 'chi'], 'variance'),
  contract('10.5', '看涨鲨鱼鳍UOC', ['U', 'S0', 'T', 'N', 'K', 'Hko', 'etako', 'Oko', 'Pi', 'chiko', 'chi']),
  contract('10.6', '看跌鲨鱼鳍DOP', ['U', 'S0', 'T', 'N', 'K', 'Hko', 'etako', 'Oko', 'Pi', 'chiko', 'chi']),
  contract('10.7', '双边鲨鱼鳍', ['U', 'S0', 'T', 'N', 'Ku', 'Kd', 'Hup', 'Hlow', 'etau', 'etad', 'alphau', 'alphad', 'Oko', 'Pi', 'priority', 'chi']),
  contract('10.8', '区间计息', ['U', 'S0', 'T', 'N', 'Hlow', 'Hup', 'Orange', 'Rmax', 'Pi', 'chi']),
];

export const contractFor = (id) => productContracts.find((item) => item.id === id) || null;

export const pricingExtrasFor = (item) => {
  if (!item) return [];
  if (item.kind === 'variance') return variancePricing;
  return item.kind === 'multi' ? multiPricing : standardPricing;
};

export const backtestExtrasFor = (item) => {
  if (!item) return [];
  if (item.kind === 'variance') return varianceBacktest;
  return item.kind === 'multi' ? multiBacktest : standardBacktest;
};

export const fullPricingInputFor = (item) => item ? [...item.payoff, ...pricingExtrasFor(item)] : [];
export const fullBacktestInputFor = (item) => item ? [...item.payoff, ...backtestExtrasFor(item)] : [];
