"""One research request's candidates, evidence and shared calculation budget.

The caller owns lifecycle and supplies a verified compute port. No engine is
imported here. Only the Host may register candidate snapshots and data versions.
"""
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from copy import deepcopy
import json
from threading import RLock
from uuid import uuid4
from runtime.research_policy import depth_policy, research_execution
from .models import EvidenceRef, RecommendationValidationError
from .evaluation_evidence import read_evaluation_records

class ResearchSession:
    def __init__(self, case, tool_port, *, calculation_allowed=True, preset_id="sequential-deliberation"):
        self.case=case
        self.preset_id=preset_id
        self.tool_port=tool_port
        self.policy=depth_policy(case.research_depth)
        self.calculation_allowed=calculation_allowed
        self.research_id='research-'+uuid4().hex
        self._lock=RLock()
        self._candidates={}
        self._results={}
        self._pending={}
        self._completed={}
        self._used=0
        self._closed=False
        self._role_candidates={}
        self._original_candidates={}
        self._pending_input = None
        self._catalog_evidence = {}
        self._retrieved_catalog_evidence = {}
        self._stage_inputs = {}

    def record_catalog_search(self, response):
        """Retain validated Host search results for this research's aggregation."""
        if response.get('catalog_version') != self.case.catalog_version:
            raise RecommendationValidationError('检索目录版本与本轮研究不一致')
        rows = response.get('evidence', ())
        if not isinstance(rows, (list, tuple)):
            raise RecommendationValidationError('检索证据必须为数组')
        items = tuple(EvidenceRef.from_mapping(row, expected_catalog_version=self.case.catalog_version)
                      for row in rows)
        with self._lock:
            if self._closed:
                raise RecommendationValidationError('研究会话已关闭')
            combined = self.merge_catalog_evidence(items)
            self._retrieved_catalog_evidence = {item.evidence_id: item for item in combined}

    def merge_catalog_evidence(self, initial):
        """One verified evidence set for initial and role-initiated searches."""
        with self._lock:
            if self._closed:
                raise RecommendationValidationError('研究会话已关闭')
            combined = dict(self._retrieved_catalog_evidence)
            for item in initial:
                if item.catalog_version != self.case.catalog_version:
                    raise RecommendationValidationError('检索目录版本与本轮研究不一致')
                previous = combined.get(item.evidence_id)
                if previous is not None and previous != item:
                    raise RecommendationValidationError('检索证据ID冲突：' + item.evidence_id)
                combined[item.evidence_id] = item
            return tuple(combined[key] for key in sorted(combined))

    def freeze_stage_input(self, role, value, *, candidate_bound=True):
        """Pin public phase history to this live research and role's current contracts."""
        import hashlib
        with self._lock:
            if self._closed:
                raise RecommendationValidationError('研究会话已关闭')
            canonical = role.removeprefix('SingleAgent.')
            candidates = self.continuation_snapshot(canonical)['candidates'] if candidate_bound else None
            encoded = json.dumps({'role': canonical, 'candidates': candidates, 'input': value},
                                 sort_keys=True, ensure_ascii=False, separators=(',', ':'))
            reference = {'research_id': self.research_id, 'role': canonical,
                         'content_hash': hashlib.sha256(encoded.encode()).hexdigest()}
            self._stage_inputs.setdefault(json.dumps(reference, sort_keys=True),
                                          {'candidates': candidates, 'input': deepcopy(value)})
            return deepcopy(reference)

    def read_stage_input(self, reference, role, result_path=()):
        with self._lock:
            canonical = role.removeprefix('SingleAgent.')
            if self._closed or not isinstance(reference, dict) or reference.get('role') != canonical:
                raise RecommendationValidationError('冻结阶段引用角色无效或研究已关闭')
            saved = self._stage_inputs.get(json.dumps(reference, sort_keys=True))
            if saved is None or reference.get('research_id') != self.research_id:
                raise RecommendationValidationError('冻结阶段引用不属于本轮研究或已被修改')
            if saved['candidates'] is not None and saved['candidates'] != self.continuation_snapshot(canonical)['candidates']:
                raise RecommendationValidationError('当前候选已改变，旧阶段引用不能代表当前条款')
            if not isinstance(result_path, (list, tuple)) or len(result_path) > 12:
                raise RecommendationValidationError('冻结阶段读取路径无效')
            value = saved['input']
            for key in result_path:
                if isinstance(value, dict) and isinstance(key, str) and key in value:
                    value = value[key]
                elif isinstance(value, list) and type(key) is int and 0 <= key < len(value):
                    value = value[key]
                else:
                    raise RecommendationValidationError('冻结阶段读取路径不存在')
            return {'status': 'completed', 'source': 'frozen_stage_input',
                    'stage_ref': deepcopy(reference), 'result_path': list(result_path), 'input': deepcopy(value),
                    'calculation_started': False}

    def catalog_for_research(self, evidence, candidates):
        """Inline selected product texts and pin the other initial search results."""
        products = {candidate.product_id for candidate in candidates}
        inline, references = [], []
        with self._lock:
            for item in evidence:
                row = item.to_dict(include_excerpt=True)
                reference = {key: row[key] for key in ("evidence_id", "catalog_version", "excerpt_hash")}
                reference["research_id"] = self.research_id
                key = json.dumps(reference, sort_keys=True)
                self._catalog_evidence.setdefault(key, deepcopy(row))
                if not row.get("product_id") or row["product_id"] in products:
                    inline.append(deepcopy(row))
                else:
                    references.append({"product_id": row["product_id"], "source": row.get("source"),
                        "read_request": {"catalog_ref": reference}})
        return {"evidence": inline, "catalog_references": references}

    def read_catalog(self, reference):
        """Return exact request-local frozen text; never fall back to new search."""
        with self._lock:
            if self._closed or not isinstance(reference, dict):
                raise RecommendationValidationError('冻结目录引用无效或研究已关闭')
            row = self._catalog_evidence.get(json.dumps(reference, sort_keys=True))
            if row is None:
                raise RecommendationValidationError('冻结目录引用不属于本轮研究或已被修改')
            return {"status": "completed", "source": "frozen_catalog_evidence",
                    "catalog_ref": deepcopy(reference), "evidence": deepcopy(row),
                    "calculation_started": False}

    def register(self, candidates):
        with self._lock:
            for candidate in candidates:
                snapshot = deepcopy(candidate.to_confirmation_dict())
                self._original_candidates.setdefault(candidate.candidate_id, snapshot)
                self._candidates[candidate.candidate_id] = snapshot

    def restrict_role(self, role, candidates):
        """Keep first-round council research independent at the tool boundary."""
        with self._lock:
            self._role_candidates[role] = {candidate.candidate_id for candidate in candidates}

    def has_registered_candidates(self, role):
        """Only Host-registered candidates unlock role calculation tools."""
        canonical = role.removeprefix('SingleAgent.')
        with self._lock:
            permitted = self._role_candidates.get(canonical)
            return any(permitted is None or candidate_id in permitted
                       for candidate_id in self._candidates)

    def share_evidence(self):
        """The workflow opens branch evidence only after independent research."""
        with self._lock:
            self._role_candidates.clear()

    def _check_role(self, role, candidate_id):
        canonical = role.removeprefix('SingleAgent.')
        permitted = self._role_candidates.get(canonical)
        if permitted is not None and candidate_id not in permitted:
            raise RecommendationValidationError('独立研究阶段只能操作本角色的候选')

    @property
    def used(self):
        with self._lock:return self._used

    def continuation_snapshot(self, role):
        """Host-owned current facts, without starting work or resetting budgets."""
        with self._lock:
            if self._closed:
                raise RecommendationValidationError('研究会话已关闭，不能重建活动上下文')
            permitted = self._role_candidates.get(role.removeprefix('SingleAgent.'))
            candidates = {key: value for key, value in self._candidates.items()
                          if permitted is None or key in permitted}
            evidence = [deepcopy(row) for row in self._results.values()
                        if row.get('candidate_id') in candidates and row.get('candidate_snapshot') == candidates[row['candidate_id']]]
            for row in evidence:
                modules = {key for key, status in row.get('module_statuses', {}).items() if status in {'succeeded', 'partial'}}
                row['module_run_refs'] = [ref for ref in row.get('module_run_refs', []) if ref.get('module') in modules]
                row['verified_metrics'] = {key: fact for key, fact in row.get('verified_metrics', {}).items()
                                           if fact.get('source') in modules | {'contract_terms'}}
            return {'research_id': self.research_id, 'candidates': deepcopy(list(candidates.values())),
                    'evidence': evidence, 'used_calculations': self._used,
                    'remaining_calculations': max(0, self.policy['calculations'] - self._used),
                    'calculation_allowed': self.calculation_allowed,
                    'pending_input': deepcopy(self._pending_input)}

    def context(self):
        with self._lock:
            return {'research_id':self.research_id,'research_depth':self.case.research_depth,
                    'remaining_calculations':max(0,self.policy['calculations']-self._used),
                    'calculation_allowed':self.calculation_allowed,
                    'candidates':deepcopy(list(self._candidates.values()))}

    def model_view(self, value):
        """Give model hosts a typed reading view, preserving stored raw inputs."""
        project = getattr(self.tool_port, 'research_model_view', None)
        return project(value) if callable(project) else deepcopy(value)

    def read(self, candidate_id=None, *, role=None):
        with self._lock:
            permitted = None if role is None else self._role_candidates.get(role.removeprefix('SingleAgent.'))
            if role is not None and candidate_id is not None:
                self._check_role(role, candidate_id)
            return deepcopy([row for row in self._results.values()
                             if (candidate_id is None or row['candidate_id']==candidate_id)
                             and (permitted is None or row['candidate_id'] in permitted)])

    def require_complete_inputs(self):
        """Surface known user-input gaps before unrelated output-format repair."""
        with self._lock:
            pending = self._pending_input
        if pending is not None:
            from .service import RecommendationInputRequired
            raise RecommendationInputRequired(*pending)

    def validate_citations(self, role, result):
        """Reject invented or stale references before accepting role handoffs.

        This checks evidence identity, not the truth of unrestricted prose.
        Semantic interpretation still needs its own acceptance cases.
        """
        self.require_complete_inputs()
        for collection in ('evaluations', 'branch_results'):
            for row in result.get(collection, ()):
                references = row.get('used_fact_refs', ())
                if not references:
                    continue
                candidate_id = row.get('candidate_id')
                with self._lock:
                    current = self._candidates.get(candidate_id)
                if current is None:
                    raise RecommendationValidationError('金融事实引用没有绑定已登记候选')
                self._check_role(role, candidate_id)
                known = {
                    fact['fact_ref']
                    for evidence in self.read(candidate_id, role=role)
                    if evidence.get('candidate_snapshot') == current
                    for fact in evidence.get('verified_metrics', {}).values()
                    if isinstance(fact, dict) and fact.get('fact_ref')
                    and (fact.get('source') == 'contract_terms'
                         or evidence.get('module_statuses', {}).get(fact.get('source')) in {'succeeded', 'partial'})
                }
                if not set(references).issubset(known):
                    raise RecommendationValidationError('角色引用了不存在、失效或不属于当前候选的金融事实')

    def read_result(self, candidate_id, module, result_path=(), *, role='Host', offset=0, limit=64, expected_run_ref=None):
        """Read a page of an already verified result, without another compute."""
        if module not in {'payoffer', 'pricer', 'backtester'}:
            raise RecommendationValidationError('读取结果的模块无效')
        if (not isinstance(result_path, (list, tuple)) or len(result_path)>12
                or any(type(key) not in {str, int} for key in result_path)):
            raise RecommendationValidationError('result_path必须为字段名或数组位置的列表')
        if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=128:
            raise RecommendationValidationError('结果分页范围无效')
        rows=self.read(candidate_id, role=role)
        with self._lock:
            current=self._candidates.get(candidate_id)
        matching=[row for row in rows if row.get('candidate_snapshot')==current
                  and row.get('module_statuses', {}).get(module) in {'succeeded','partial'}]
        if expected_run_ref is not None:
            if not isinstance(expected_run_ref, dict) or expected_run_ref.get('module') != module:
                raise RecommendationValidationError('冻结结果引用无效')
            matching = [row for row in matching if any(
                ref == expected_run_ref for ref in row.get('module_run_refs', ()))]
            if not matching:
                raise RecommendationValidationError('指定冻结结果与当前候选不一致或已不可用，不能改读其他运行')
        if not matching:
            if current is None:
                raise RecommendationValidationError('读取结果必须指定已登记候选')
            return {'status': 'completed', 'available': False,
                    'candidate_id': candidate_id, 'module': module, 'evidence': [],
                    'calculation_started': False,
                    'message': '当前候选尚无适用计算结果。只有本轮允许计算且确有需要时才发起计算；否则明确说明尚未验证。'}
        reference=next((ref for ref in matching[-1]['module_run_refs'] if ref['module']==module),None)
        reader=getattr(self.tool_port,'read_research_result',None)
        if reference is None or not callable(reader):
            raise RecommendationValidationError('当前宿主尚未提供结果读取能力')
        node=reader(deepcopy(reference))
        for key in result_path:
            if isinstance(node,dict) and isinstance(key,str) and key in node:
                node=node[key]
            elif isinstance(node,list) and type(key) is int and 0<=key<len(node):
                node=node[key]
            else:
                if isinstance(node, dict):
                    available = json.dumps(list(node)[:32], ensure_ascii=False)
                    detail = f'当前层可用字段：{available}'
                elif isinstance(node, list):
                    detail = f'当前层为数组，长度为{len(node)}，使用有效整数下标'
                else:
                    detail = '当前层为单值，不能继续读取子字段'
                raise RecommendationValidationError(
                    f'结果中不存在指定字段或数组位置。{detail}。请按字段层级读取，不重新计算。'
                )
        # Page by both item count and text size. Large child sections remain in
        # the frozen result and are reached through explicit field paths.
        if isinstance(node, (dict, list)):
            total = len(node)
            items = list(node.items()) if isinstance(node, dict) else list(enumerate(node))
            value = {} if isinstance(node, dict) else []
            deferred = []
            consumed = 0
            value_indices = []
            size = 0
            for key, item in items[offset:offset+limit]:
                encoded = json.dumps(item, ensure_ascii=False, separators=(',', ':'))
                locator = None
                if isinstance(item, (dict, list)) and len(encoded) > 1_200:
                    locator = {'field': key, 'result_path': [*result_path, key],
                               'type': 'object' if isinstance(item, dict) else 'array',
                               'item_count': len(item)}
                cost = len(json.dumps(locator, ensure_ascii=False)) if locator else len(encoded) + len(str(key)) + 8
                if consumed and size + cost > 2_000:
                    break
                if locator:
                    deferred.append(locator)
                elif isinstance(value, dict):
                    value[key] = deepcopy(item)
                else:
                    value.append(deepcopy(item))
                    value_indices.append(key)
                size += cost
                consumed += 1
            next_offset = offset + consumed if offset + consumed < total else None
        else:
            total = 1
            value = deepcopy(node) if offset == 0 else None
            deferred = []
            next_offset = None
        page = {'status':'completed','module_run_ref':reference,'result_path':list(result_path),
                'offset':offset,'total':total,'next_offset':next_offset,
                'value':value,'source':'verified_frozen_result','calculation_started':False}
        if deferred:
            page['deferred_fields'] = deferred
            if isinstance(node, list):
                page['value_indices'] = value_indices
            page['value_complete'] = False
            page['message'] = '大字段未在本页展开。按deferred_fields中的result_path读取所需字段；完整原始结果仍保留，未重新计算。'
        return page

    def evaluate(self, request, *, role='Host'):
        allowed={'candidate_id','modules','question','term_overrides','round_no'}
        if not isinstance(request,dict) or set(request)-allowed:
            raise RecommendationValidationError('研究请求含未声明字段')
        question=request.get('question')
        if not isinstance(question,str) or not question.strip():
            raise RecommendationValidationError('需要明确本次计算要验证的问题')
        candidate_id=request.get('candidate_id')
        modules=request.get('modules')
        if not isinstance(modules,list) or not modules or len(set(modules))!=len(modules) or any(m not in {'payoffer','pricer','backtester'} for m in modules):
            raise RecommendationValidationError('研究modules无效')
        with self._lock:
            if self._closed:raise RecommendationValidationError('本次研究已结束')
            if candidate_id not in self._candidates:raise RecommendationValidationError('请先提交候选，由Host登记后再计算')
            self._check_role(role, candidate_id)
            candidate=deepcopy(self._candidates[candidate_id])
        if not self.calculation_allowed:
            return self._unavailable(candidate,modules,'用户要求只比较，不进行计算')
        overrides=request.get('term_overrides',{})
        if not isinstance(overrides,dict):raise RecommendationValidationError('term_overrides必须为对象')
        # Candidate term changes must go through the existing variant validator;
        # no tool-call argument may silently mutate the registered snapshot.
        current=candidate.get('current_inputs',{}).get('term_overrides',{})
        if any(current.get(key)!=value for key,value in overrides.items()):
            raise RecommendationValidationError('条款变体必须先交给设计角色并由Host登记，不能覆盖当前候选')
        round_no=request.get('round_no',1)
        if type(round_no) is not int or round_no<1:raise RecommendationValidationError('研究轮次无效')
        # Without a Host-frozen data identity, successful runs are not cached.
        # Concurrent requests may share an in-flight execution only.
        identify=getattr(self.tool_port,'research_data_identity',None)
        data_before=identify() if callable(identify) else None
        key=json.dumps({'candidate':candidate,'modules':sorted(modules),
                        'constraints':self.case.confirmed_constraints,'data_identity':data_before},sort_keys=True,ensure_ascii=False,allow_nan=False)
        with self._lock:
            if data_before is not None and key in self._completed:
                return self._reuse(self._completed[key], question, role, round_no)
            future=self._pending.get(key)
            owner=future is None
            if owner:
                if self._used+len(modules)>self.policy['calculations']:
                    return self._unavailable(candidate,modules,'研究计算预算已用完，尚未验证')
                future=Future();self._pending[key]=future
                self._used+=len(modules)
        if not owner:
            while True:
                with self._lock:
                    if self._closed:
                        raise RecommendationValidationError('研究已停止，不再等待重复计算')
                try:
                    response = future.result(timeout=0.1)
                    return self._reuse(response, question, role, round_no)
                except FutureTimeoutError:
                    continue
        try:
            with research_execution({"research_id":self.research_id,"question":question,"role":role,
                                     "candidate_snapshot":candidate,"purpose":"research"}):
                response=dict(self.tool_port.evaluate_candidate(candidate=candidate,
                    confirmed_constraints=dict(self.case.confirmed_constraints),modules=modules,
                    term_overrides=dict(current),candidate_id=candidate_id,round_no=round_no))
            read_evaluation_records(response,candidate_id=candidate_id,modules=modules,round_no=round_no,
                                    tenant_id=self.case.tenant_id,task_id=self.case.task_id)
            evidence_id='evidence-'+uuid4().hex
            response.update({'research_id':self.research_id,'research_evidence_id':evidence_id,
                             'question':question,'requested_by':role,'candidate_snapshot':candidate,
                             'purpose':'research'})
            data_after=identify() if callable(identify) else None
            with self._lock:
                self._results[evidence_id]=deepcopy(response)
                if data_before is not None and data_before==data_after and all(status=="succeeded" for status in response["module_statuses"].values()):
                    self._completed[key]=deepcopy(response)
            future.set_result(deepcopy(response))
            return response
        except BaseException as error:
            from .service import RecommendationInputRequired
            if isinstance(error, RecommendationInputRequired):
                with self._lock:
                    self._pending_input = (error.field, error.question)
                response = self._unavailable(candidate, modules, error.question)
                response.update(status='needs_input', missing_information=[error.field],
                                next_question=error.question)
                future.set_result(deepcopy(response))
                return response
            future.set_exception(error)
            raise
        finally:
            with self._lock:self._pending.pop(key,None)

    def _reuse(self, original, question, role, round_no):
        response = deepcopy(original)
        response.update(question=question, requested_by=role, round_no=round_no, reused=True)
        if 'research_evidence_id' not in original:
            return response
        response['reused_evidence_id'] = original['research_evidence_id']
        response['research_evidence_id'] = 'evidence-'+uuid4().hex
        # Records describe the current evidence consumption, while RunRefs and
        # original evidence retain the unchanged computation and its provenance.
        response.pop('evaluation_records', None)
        with self._lock:
            self._results[response['research_evidence_id']] = deepcopy(response)
        return response

    def _unavailable(self,candidate,modules,message):
        return {'status':'partial','candidate_id':candidate['candidate_id'],
                'product_id':candidate['product_id'],'rule_revision':candidate['rule_revision'],
                'module_statuses':{m:'unsupported' for m in modules},'module_run_refs':[],
                'verified_metrics':{},'message':message,'research_id':self.research_id}

    def close(self):
        with self._lock:self._closed=True

    def result_for(self, candidate, modules, round_no):
        snapshot=candidate.to_confirmation_dict()
        rows=[row for row in self.read(candidate.candidate_id) if row.get('candidate_snapshot')==snapshot]
        statuses={module:'unsupported' for module in modules}
        refs={}
        metrics={}
        for row in rows:
            for module,status in row.get('module_statuses',{}).items():
                if module not in statuses:continue
                statuses[module]=status
                refs.pop(module,None)
                if status in {'succeeded','partial'}:
                    reference=next((ref for ref in row.get('module_run_refs',[]) if ref.get('module')==module),None)
                    if reference is not None:refs[module]=reference
            for metric,fact in row.get('verified_metrics',{}).items():
                if fact.get('source') in modules or fact.get('source')=='contract_terms':metrics[metric]=fact
        # Never retain metrics from a failed latest module.
        metrics={key:fact for key,fact in metrics.items() if fact.get('source')=='contract_terms' or statuses.get(fact.get('source')) in {'succeeded','partial'}}
        return {'candidate_id':candidate.candidate_id,'product_id':candidate.product_id,
                'rule_revision':candidate.rule_revision,'round_no':round_no,'module_statuses':statuses,
                'module_run_refs':list(refs.values()),'verified_metrics':metrics,
                'status':'completed' if all(value=='succeeded' for value in statuses.values()) else 'partial',
                'message':'部分研究尚未取得可用证据' if any(value=='unsupported' for value in statuses.values()) else ''}
