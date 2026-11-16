"""The preserved consumer reads documents independently of gwflow's serializer."""

import json
import unittest

from fixtures.status_consumer_v1 import consume, check_handoff, job_outcome


class StatusConsumerTests(unittest.TestCase):
    def test_empty_success_is_distinct_from_an_inspection_error(self):
        report = {"schema_version": 1, "tasks": [], "notices": []}
        self.assertEqual(consume(json.dumps(report), details=False, exit_code=0), report)
        error = {"schema_version": 1, "error": {"code": "inspection_failed", "message": "Unavailable"}}
        self.assertEqual(consume(json.dumps(error), details=False, exit_code=1), error)
        with self.assertRaises(ValueError):
            consume(json.dumps(error), details=False, exit_code=0)

    def test_required_fields_and_types_are_not_silently_accepted(self):
        report = completed_report()
        consume(json.dumps(report), details=True, exit_code=0)
        paths = [(), ('tasks', 0), ('tasks', 0, 'progress'), ('tasks', 0, 'reason'),
                 ('tasks', 0, 'jobs', 0), ('notices', 0)]
        for path in paths:
            original = report
            for key in path:
                original = original[key]
            for name in original:
                for change in ('remove', 'rename', 'type'):
                    with self.subTest(path=path, name=name, change=change):
                        changed = json.loads(json.dumps(report))
                        target = changed
                        for key in path:
                            target = target[key]
                        value = target.pop(name)
                        if change == 'rename':
                            target['renamed_' + name] = value
                        elif change == 'type':
                            target[name] = [] if type(value) is not list else {}
                        with self.assertRaises(ValueError):
                            consume(json.dumps(changed), details=True, exit_code=0)

    def test_additions_preserve_unknown_values_and_messages_without_claiming_inactivity(self):
        report = completed_report()
        task = report['tasks'][0]
        task.update(state='future_state', next_action='future_action', extra={'new': 1})
        task['reason'] = {'code': 'future_reason', 'message': 'New explanation', 'extra': True}
        task['jobs'][0].update(state='future_job', observed_state='future_observation', extra=1)
        report.update(extra=['new'])
        observed = consume(json.dumps(report), details=True, exit_code=0)
        self.assertEqual(observed, report)
        self.assertEqual(job_outcome(observed['tasks'][0]['jobs'][0]),
                         {'success': False, 'inactive': False})
        for state in ('unknown', 'pending', 'running', 'queued'):
            self.assertEqual(job_outcome({'state': state}), {'success': False, 'inactive': False})
        self.assertEqual(job_outcome({'state': 'completed'}), {'success': True, 'inactive': True})
        error = {'schema_version': 1, 'error': {'code': 'future_error', 'message': 'New error', 'extra': 1}}
        self.assertEqual(consume(json.dumps(error), details=False, exit_code=1), error)

    def test_cleaned_handoff_meanings_and_recorded_identity_are_preserved(self):
        report = completed_report()
        report['notices'] = []
        identities = {job['name']: (job['submission_name'], job['backend_id']) for job in report['tasks'][0]['jobs']}
        check_handoff(report, case='cleaned', attempt_id='attempt-A', identities=identities)
        for path, value in ((('state',), 'pending'), (('next_action',), 'run'),
                            (('attempt_id',), 'different'), (('progress', 'completed'), 0),
                            (('reason', 'code'), 'no_attempt'), (('jobs', 0, 'observed_state'), None),
                            (('jobs', 0, 'submission_name'), None), (('jobs', 0, 'backend_id'), None),
                            (('jobs', 0, 'in_current_definition'), False)):
            with self.subTest(path=path):
                changed = json.loads(json.dumps(report))
                target = changed['tasks'][0]
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = value
                with self.assertRaises(ValueError):
                    check_handoff(changed, case='cleaned', attempt_id='attempt-A', identities=identities)

    def test_compatible_additions_pass_the_preserved_handoff_assertions(self):
        report = completed_report()
        task = report['tasks'][0]
        identities = {job['name']: (job['submission_name'], job['backend_id']) for job in task['jobs']}
        for record in (report, task, task['progress'], task['reason'], *task['jobs'], *report['notices']):
            record['future_field'] = {'information': 'preserve me'}
        observed = consume(json.dumps(report), details=True, exit_code=0)
        self.assertEqual(observed, report)
        check_handoff(observed, case='cleaned', attempt_id='attempt-A', identities=identities)

    def test_partial_handoff_keeps_successful_work_separate_from_retry(self):
        report = completed_report()
        report['notices'] = []
        task = report['tasks'][0]
        task.update(name='sample', state='failed', next_action='retry',
                    progress={'completed': 2, 'total': 5},
                    reason={'code': 'jobs_require_retry', 'message': 'Some computation needs retry'})
        task['jobs'] = [dict(name='sample__' + name, submission_name='submission-' + name,
                            backend_id=str(index), state=state, observed_state=state,
                            in_current_definition=True)
                        for index, (name, state) in enumerate((('gwflow_prepare', 'completed'),
                            ('left', 'completed'), ('right', 'failed'), ('join', 'failed'),
                            ('gwflow_complete', 'failed')))]
        identities = {job['name']: (job['submission_name'], job['backend_id']) for job in task['jobs']}
        check_handoff(consume(json.dumps(report), details=True, exit_code=0),
                      case='partial', attempt_id='attempt-A', identities=identities)
        for path, value in ((('state',), 'reusable'), (('next_action',), 'reuse'),
                            (('progress', 'completed'), 5), (('jobs', 1, 'state'), 'pending'),
                            (('jobs', 2, 'observed_state'), 'completed')):
            with self.subTest(path=path):
                changed = json.loads(json.dumps(report))
                target = changed['tasks'][0]
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = value
                with self.assertRaises(ValueError):
                    check_handoff(changed, case='partial', attempt_id='attempt-A', identities=identities)

    def test_nulls_omissions_and_error_envelopes(self):
        report = completed_report()
        task = report['tasks'][0]
        task['attempt_id'] = None
        task['progress']['completed'] = None
        for job in task['jobs']:
            for key in ('submission_name', 'backend_id', 'observed_state'):
                job[key] = None
        self.assertEqual(consume(json.dumps(report), details=True, exit_code=0), report)
        with self.assertRaises(ValueError):
            consume(json.dumps(report), details=False, exit_code=0)
        del task['jobs']
        consume(json.dumps(report), details=False, exit_code=0)
        with self.assertRaises(ValueError):
            consume(json.dumps(report), details=True, exit_code=0)
        for value in (True, 1.0, '1'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                consume(json.dumps({**report, 'schema_version': value}), details=False, exit_code=0)
        for document in ({'schema_version': 1, 'error': {'code': 'usage_error'}},
                         {'schema_version': 1, 'error': {'code': 4, 'message': 'bad'}},
                         {**report, 'error': {'code': 'inspection_failed', 'message': 'bad'}}):
            with self.subTest(document=document), self.assertRaises(ValueError):
                consume(json.dumps(document), details=False, exit_code=1)
        with self.assertRaises(ValueError):
            consume(json.dumps(report) + '{}', details=False, exit_code=0)


def completed_report():
    # Hand-written example: one computation plus preparation and completion.
    # The notice is deliberately unrelated to Task success.
    return {'schema_version': 1, 'tasks': [{
        'name': 'cleaned', 'attempt_id': 'attempt-A', 'state': 'reusable', 'next_action': 'reuse',
        'progress': {'completed': 3, 'total': 3},
        'reason': {'code': 'results_valid', 'message': 'Results can be reused'},
        'jobs': [{'name': 'cleaned__' + name, 'submission_name': 'submission-' + name,
                  'backend_id': str(index), 'state': 'completed', 'observed_state': 'completed',
                  'in_current_definition': True}
                 for index, name in enumerate(('gwflow_prepare', 'copy', 'gwflow_complete'))],
    }], 'notices': [{'code': 'future_notice', 'message': 'Keep this visible', 'task_names': ['another']} ]}
