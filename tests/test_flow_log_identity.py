import unittest
from unittest.mock import patch

from pipeline.flow.service import FlowService


class FlowLogIdentityTest(unittest.TestCase):
    def test_log_snapshots_email_without_mutating_details(self):
        details = {'reason': 'Quota exceeded'}
        with patch('pipeline.flow.service.store.get_row', return_value={'email': 'saved@example.com'}), patch(
            'pipeline.flow.service.store.put_row'
        ) as put, patch('pipeline.flow.service.store.list_rows', return_value=[]):
            FlowService()._log('warning', 'account_suspended', account_id='account-1', details=details)
        self.assertEqual(put.call_args.args[1]['details']['accountEmail'], 'saved@example.com')
        self.assertEqual(details, {'reason': 'Quota exceeded'})

    def test_old_logs_resolve_email_and_preserve_snapshot_after_account_changes(self):
        rows = [
            {'id': 'legacy', 'createdAt': 1, 'accountId': 'account-1'},
            {'id': 'saved', 'createdAt': 2, 'accountId': 'account-1', 'details': {'accountEmail': 'old@example.com'}},
            {'id': 'deleted', 'createdAt': 3, 'accountId': 'deleted'},
        ]
        def account(_table, account_id):
            return {'email': 'current@example.com'} if account_id == 'account-1' else None
        with patch('pipeline.flow.service.store.list_rows', return_value=rows), patch(
            'pipeline.flow.service.store.get_row', side_effect=account
        ):
            result = FlowService().logs()
        self.assertEqual([r['id'] for r in result], ['deleted', 'saved', 'legacy'])
        self.assertEqual(result[0]['details'], {})
        self.assertEqual(result[1]['details']['accountEmail'], 'old@example.com')
        self.assertEqual(result[2]['details']['accountEmail'], 'current@example.com')
        self.assertNotIn('details', rows[0])


if __name__ == '__main__':
    unittest.main()
