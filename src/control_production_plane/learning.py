"""Explicit P learning-candidate delivery through public K operations.

Publish one frozen production observation, then propose (never review/admit).
Both exact requests precede external I/O; receipts are acknowledged separately.
"""
import hashlib
import json

from . import model as m
from .knowledge import KnowledgeAdapter, context_value, encoded, feedback_material, historical

SOURCE_FIELDS = ('candidate_id', 'run_id', 'attempt_id', 'explanation', 'evidence_refs',
                 'artifact_versions', 'goal_digest', 'revision', 'operation_id', 'binding')
OPERATIONS = ('publish_material', 'propose')


def source(candidate):
    return {name: candidate[name] for name in SOURCE_FIELDS}


def proposal_spec(value):
    m.validate_tree(value)
    m.fields(value, 'kind content conditions context gaps material_ids')
    for key, maximum in (('kind', 64), ('content', 65536)):
        m.text(value[key], maximum)
        m.require('\x00' not in value[key], 'K text cannot contain NUL')
    for key in ('conditions', 'context'):
        context_value(value[key])
    m.require(type(value['gaps']) is list and len(value['gaps']) <= 15, 'At most 15 caller gaps')
    for gap in value['gaps']:
        m.text(gap, 512)
        m.require('\x00' not in gap, 'K gaps cannot contain NUL')
    mids = value['material_ids']
    m.require(type(mids) is list and len(mids) <= 7, 'At most seven additional K materials')
    for mid in mids:
        m.identity(mid)
        m.require(len(mid) >= 3, 'K identities must contain at least three characters')
    m.require(len(set(mids)) == len(mids), 'Duplicate additional material')
    return json.loads(m.canonical(value))


def load(db, identity):
    m.identity(identity)
    row = db.execute('SELECT document FROM candidates WHERE candidate_id=?', (identity,)).fetchone()
    m.require(row is not None, 'Learning candidate not found', 'NOT_FOUND')
    return m.decode(row[0].encode('ascii'))


def expected_result(request):
    if request['operation'] == 'publish_material':
        return dict(material_id=request['body']['material_id'], reused=False)
    return dict(candidate_id='x' * 128, candidate_sha256=hashlib.sha256(encoded(request['body'])).hexdigest(), reused=False)


def capacity(candidate):
    """Reserve both bounded receipts before any external write."""
    projected = json.loads(m.canonical(candidate))
    record = projected['delivery_record']
    for op in OPERATIONS:
        req = record['requests'][op]
        record['receipts'][op] = dict(store_id=record['knowledge_store_id'], scope=req['scope'],
            operation=op, request_id=req['request_id'], fingerprint='0' * 64,
            recorded_at=9007199254740991, result=expected_result(req))
    projected['delivery'] = 'RECEIVED'
    m.validate_tree(projected)


def save(db, candidate):
    m.validate_tree(candidate)
    capacity(candidate)
    db.execute('UPDATE candidates SET document=? WHERE candidate_id=?',
               (m.canonical(candidate).decode(), candidate['candidate_id']))


def record_intent(store, db, body):
    m.fields(body, 'candidate_id knowledge_store_id scope proposal')
    m.identity(body['knowledge_store_id'])
    m.identity(body['scope'])
    spec = proposal_spec(body['proposal'])
    candidate = load(db, body['candidate_id'])
    old = candidate.get('delivery_record')
    if old:
        m.require(old['knowledge_store_id'] == body['knowledge_store_id'] and old['scope'] == body['scope']
                  and old['proposal'] == spec and old['source_sha256'] == m.digest(source(candidate)),
                  'Original delivery binds different destination or content', 'CONFLICT')
        capacity(candidate)
        return dict(run_id=candidate['run_id'], candidate_id=candidate['candidate_id'])
    current = store._load(db, candidate['run_id'])
    original = historical(store, db, current, candidate['revision'])
    m.require(original['attempt']['attempt_id'] == candidate['attempt_id']
              and original['attempt']['operation_id'] == candidate['operation_id']
              and original['goal_digest'] == candidate['goal_digest'] and original['binding'] == candidate['binding'],
              'Candidate differs from its original production attempt', 'IDENTITY_CONFLICT')
    accepted = original['attempt'].get('candidate')
    for ref in candidate['artifact_versions']:
        m.require(accepted is not None and all(ref[k] == accepted[k] for k in ('artifact_id', 'version_id')),
                  'Delivery artifact must match the recorded production candidate', 'CONFLICT')
    identity = lambda name: 'pl:' + m.digest([store.identity['store_id'], candidate['candidate_id'], name])
    material = feedback_material(store.identity['store_id'], original,
        dict(consumer_ref='p:' + store.identity['store_id'], task_ref='p-task:' + candidate['operation_id'],
             context=spec['context']), 'Production snapshot for an explicitly supplied learning candidate.')
    # References are not copied originals or independent evidence of causation.
    facts = dict(schema='production-learning-material-v1', learning_candidate=source(candidate),
                 production=json.loads(material['text']))
    text = encoded(facts).decode()
    m.require(len(text.encode()) <= 65536, 'Learning material exceeds K text limit', 'LIMIT')
    material.update(material_id=identity('material'), text=text, gaps=spec['gaps'] + [
        'P records the observed attempt and caller-supplied explanation/references; admission and causal attribution require review.'])
    m.require(material['material_id'] not in spec['material_ids'], 'Generated material is already listed')
    requests = {}
    for op, value in (('publish_material', material), ('propose', dict(proposal_id=identity('proposal'),
        kind=spec['kind'], content=spec['content'], conditions=spec['conditions'],
        material_ids=[material['material_id']] + spec['material_ids']))):
        requests[op] = dict(schema='operational-knowledge-v1', scope=body['scope'], operation=op,
                           request_id=identity(op), body=value)
        m.validate_tree(requests[op])
    candidate['delivery_record'] = dict(schema='production-learning-delivery-v1',
        knowledge_store_id=body['knowledge_store_id'], scope=body['scope'], proposal=spec,
        source_sha256=m.digest(source(candidate)), requests=requests, receipts={op: None for op in OPERATIONS})
    candidate['delivery'] = 'PENDING'
    save(db, candidate)
    return dict(run_id=candidate['run_id'], candidate_id=candidate['candidate_id'])


def record_ack(store, db, body):
    m.fields(body, 'candidate_id operation receipt')
    candidate = load(db, body['candidate_id'])
    record = candidate.get('delivery_record')
    op = body['operation']
    m.require(record is not None and op in OPERATIONS, 'Original delivery intent missing', 'NOT_FOUND')
    m.require(record['source_sha256'] == m.digest(source(candidate)), 'Learning source changed', 'IDENTITY_CONFLICT')
    req, receipt = record['requests'][op], body['receipt']
    m.fields(receipt, 'store_id scope operation request_id fingerprint recorded_at result')
    m.require(receipt['store_id'] == record['knowledge_store_id']
              and all(receipt[k] == req[k] for k in ('scope', 'operation', 'request_id'))
              and receipt['fingerprint'] == hashlib.sha256(encoded(req)).hexdigest(),
              'K receipt differs from original learning request', 'IDENTITY_CONFLICT')
    m.natural(receipt['recorded_at'], low=0, high=9007199254740991)
    result = receipt['result']
    m.fields(result, 'material_id reused' if op == 'publish_material' else 'candidate_id candidate_sha256 reused')
    m.require(type(result['reused']) is bool, 'Invalid reuse flag')
    if op == 'publish_material':
        m.require(result['material_id'] == req['body']['material_id'], 'Material identity differs', 'IDENTITY_CONFLICT')
    else:
        m.require(record['receipts']['publish_material'] is not None, 'Material receipt not recorded', 'NOT_READY')
        m.identity(result['candidate_id'])
        m.require(result['candidate_sha256'] == hashlib.sha256(encoded(req['body'])).hexdigest(),
                  'Candidate content digest differs', 'IDENTITY_CONFLICT')
    m.require(record['receipts'][op] is None or record['receipts'][op] == receipt,
              'Original learning receipt changed', 'CONFLICT')
    record['receipts'][op] = receipt
    candidate['delivery'] = 'RECEIVED' if record['receipts']['propose'] is not None else 'PENDING'
    save(db, candidate)
    return dict(run_id=candidate['run_id'], candidate_id=candidate['candidate_id'], delivery=candidate['delivery'])


class LearningAdapter(KnowledgeAdapter):
    def deliver(self, candidate_id, proposal):
        proposal = proposal_spec(proposal)
        with self.store.executor() as lease:
            self._open()  # Verify destination before retaining a new intent.
            body = dict(candidate_id=candidate_id, knowledge_store_id=self.config['store_id'],
                        scope=self.config['scope'], proposal=proposal)
            self.store.execute(self._command('learning_intent', body, 'intent'), lease=lease)
            return self._resume_delivery(candidate_id, lease)

    def resume_delivery(self, candidate_id):
        with self.store.executor() as lease:
            return self._resume_delivery(candidate_id, lease)

    def _resume_delivery(self, candidate_id, lease):
        from domain_operational_knowledge_core import KnowledgeError
        candidate = self.store.get_candidate(candidate_id)
        record = candidate.get('delivery_record')
        m.require(record is not None, 'Original learning intent missing', 'NOT_FOUND')
        m.require(record['knowledge_store_id'] == self.config['store_id'] and record['scope'] == self.config['scope'],
                  'Original learning destination changed', 'IDENTITY_CONFLICT')
        capacity(candidate)
        k = self._open()
        for op in OPERATIONS:
            candidate = self.store.get_candidate(candidate_id)
            record = candidate['delivery_record']
            m.require(record['source_sha256'] == m.digest(source(candidate)), 'Learning source changed', 'IDENTITY_CONFLICT')
            req = record['requests'][op]
            try:
                receipt = k.get_operation(req['scope'], op, req['request_id'])['receipt']
            except KnowledgeError as exc:
                if exc.code != 'NOT_FOUND':
                    raise
                m.require(record['receipts'][op] is None, 'Previously committed K receipt missing', 'IDENTITY_CONFLICT')
                # K guarantees same-key recovery, including an unfinished material publication.
                # No production action is submitted by this adapter.
                receipt = k.execute(encoded(req))['receipt']
            body = dict(candidate_id=candidate_id, operation=op, receipt=receipt)
            self.store.execute(self._command('learning_ack', body, 'ack'), lease=lease)
        final = self.store.get_candidate(candidate_id)
        return dict(candidate_id=candidate_id, delivery=final['delivery'], admission=final['admission'],
                    receipts=final['delivery_record']['receipts'])
