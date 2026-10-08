import json
import os
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sms_integration as sms


class SmsTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            'SMS_WEBHOOK_SECRET': 'test-secret', 'SMS_SENDING_ENABLED': 'false',
            'COMPLETESMS_ACCOUNT_ID': 'test', 'COMPLETESMS_USERNAME': 'test@example.com',
            'COMPLETESMS_API_PASSWORD': 'test-only'}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        app = Mock()
        app.route.return_value = lambda fn: setattr(self, 'handler', fn) or fn
        self.conn = Mock()
        self.cursor = self.conn.cursor.return_value
        self.client = SimpleNamespace(Id=1, FirstName='Alex', Phone='0411222333',
            Status='Pending Team Call', SubmittedAt=datetime.now(timezone.utc).replace(tzinfo=None)-timedelta(days=3), GHLContactId='abc')
        self.cursor.fetchone.side_effect = [self.client]
        self.status = Mock(return_value={'missingDocuments': ['id']})
        self.connect = Mock(return_value=self.conn)
        sms.register_sms(app, self.connect, self.status, lambda: {})
        self.get = patch.object(sms.requests, 'get').start()
        self.addCleanup(patch.stopall)
        self.get.return_value.json.return_value = {
            'contact': {'id': 'abc', 'dnd': False, 'phone': '+61411222333'}}
        self.post = patch.object(sms.requests, 'post').start()

    def request(self, **values):
        body = {'clientId': 'CL-TEST', 'stage': 'reminder1', **values}
        req = SimpleNamespace(headers={'Authorization': 'Bearer test-secret'}, get_json=lambda: body)
        response = self.handler(req)
        return response.status_code, json.loads(response.get_body())

    def test_bad_auth_never_accesses_database(self):
        result = self.handler(SimpleNamespace(headers={}))
        self.assertEqual(result.status_code, 401)
        self.connect.assert_not_called()

    def test_disabled_sending_is_preview_even_with_dryrun_false(self):
        code, body = self.request(dryRun=False)
        self.assertEqual(body['state'], 'preview')
        self.assertIn('Alex', body['message'])
        self.post.assert_not_called()

    def test_submission_preview_without_upload_or_missing_documents(self):
        self.status.return_value = {'missingDocuments': []}
        code, body = self.request(stage='submission')
        self.assertEqual(code, 200)
        self.assertEqual(body['state'], 'preview')
        self.assertIn('submitting your scenario', body['message'])
        self.post.assert_not_called()

    def test_submission_is_not_subject_to_reminder_delay(self):
        os.environ['SMS_SENDING_ENABLED'] = 'true'
        self.client.SubmittedAt = datetime.now(timezone.utc).replace(tzinfo=None)
        self.status.return_value = {'missingDocuments': []}
        self.cursor.fetchone.side_effect = [self.client, (1,), None, (8,)]
        self.post.return_value.status_code = 200
        self.post.return_value.json.return_value = {'Result': '0000', 'Messages': {'1': {'Result': '0000', 'SMSId': 123}}}
        self.assertEqual(self.request(stage='submission', dryRun=False)[1]['state'], 'queued')
        self.post.assert_called_once()

    def test_missing_document_id_is_rejected(self):
        self.assertEqual(self.request(stage='received')[0], 400)
        self.connect.assert_not_called()

    def test_completed_file_does_not_send(self):
        self.status.return_value = {'missingDocuments': []}
        self.assertEqual(self.request()[1]['reason'], 'no_outstanding_documents')
        self.post.assert_not_called()

    def test_dnd_prevents_send(self):
        self.get.return_value.json.return_value['contact']['dnd'] = True
        self.assertEqual(self.request()[1]['state'], 'skipped')
        self.post.assert_not_called()

    def test_absent_dnd_uses_documented_false_default(self):
        self.assertFalse(sms.blocked_contact({}))
        self.assertTrue(sms.blocked_contact({'dnd': None}))
        self.assertTrue(sms.blocked_contact({'dnd': 'false'}))
        self.assertTrue(sms.blocked_contact({'tags': ['sms-opt-out']}))
        self.assertTrue(sms.blocked_contact({'dndSettings': {'SMS': {'status': 'permanent'}}}))
        self.assertTrue(sms.blocked_contact({'dnd': False, 'dndSettings': {'SMS': {'status': 'active'}}}))

    def test_missing_dnd_reaches_preview_without_sending(self):
        del self.get.return_value.json.return_value['contact']['dnd']
        self.assertEqual(self.request()[1]['state'], 'preview')
        self.post.assert_not_called()

    def test_missing_ghl_phone_is_explicitly_blocked(self):
        del self.get.return_value.json.return_value['contact']['phone']
        self.assertEqual(self.request()[1]['reason'], 'missing_ghl_phone')
        self.post.assert_not_called()

    def test_phone_mismatch_blocks(self):
        self.get.return_value.json.return_value['contact']['phone'] = '+61499999999'
        self.assertEqual(self.request()[1]['reason'], 'phone_mismatch')

    def test_duplicate_never_reposts(self):
        os.environ['SMS_SENDING_ENABLED'] = 'true'
        self.cursor.fetchone.side_effect = [self.client, (1,), SimpleNamespace(State='unknown')]
        self.assertEqual(self.request(dryRun=False)[1]['state'], 'duplicate')
        self.post.assert_not_called()

    def test_early_reminder_is_blocked(self):
        os.environ['SMS_SENDING_ENABLED'] = 'true'
        self.client.SubmittedAt = datetime.now(timezone.utc).replace(tzinfo=None)
        self.cursor.fetchone.side_effect = [self.client, (1,), None]
        self.assertEqual(self.request(dryRun=False)[1]['reason'], 'not_due')
        self.post.assert_not_called()

    def test_missing_previous_reminder_is_blocked(self):
        os.environ['SMS_SENDING_ENABLED'] = 'true'
        self.cursor.fetchone.side_effect = [self.client, (1,), None, None]
        self.assertEqual(self.request(stage='reminder2', dryRun=False)[1]['reason'], 'previous_reminder_not_queued')
        self.post.assert_not_called()

    def test_provider_timeout_reserved_and_not_retried(self):
        os.environ['SMS_SENDING_ENABLED'] = 'true'
        self.cursor.fetchone.side_effect = [self.client, (1,), None, (7,)]
        self.post.side_effect = sms.requests.Timeout()
        self.assertEqual(self.request(dryRun=False)[1]['state'], 'unknown')
        self.post.assert_called_once()
        updates = [c for c in self.cursor.execute.call_args_list if 'UPDATE dbo.ClientSmsEvents' in c.args[0]]
        self.assertEqual(updates[0].args[1:], ('unknown', None, 7))

    def test_success_requires_per_message_result(self):
        self.post.return_value.status_code = 200
        self.post.return_value.json.return_value = {'Result': '0000', 'Messages': {'1': {'Result': '2011'}}}
        self.assertEqual(sms.send_sms('+61411222333', 'Test')[0], 'rejected')
        self.post.return_value.json.return_value = {'Result': '0000', 'Messages': {'1': {'Result': '0000', 'SMSId': 123}}}
        self.assertEqual(sms.send_sms('+61411222333', 'Test'), ('queued', '123'))

    def test_phone_normalization_and_invalid_values(self):
        self.assertEqual(sms.phone_number('0411 222 333'), '+61411222333')
        with self.assertRaises(ValueError):
            sms.phone_number('not a phone')


if __name__ == '__main__':
    unittest.main()
