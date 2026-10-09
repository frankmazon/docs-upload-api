import os
import unittest
from unittest.mock import Mock, patch
import function_app as f


class ReferrerNotificationTests(unittest.TestCase):
    def notify(self, phone='', trigger_success=True):
        ref={'referrerId':'RF-TEST','referralCode':'REF-TEST','firstName':'Pat',
             'lastName':'Test','email':'pat@example.com','phone':phone,'created':True}
        response=Mock(status_code=201,content=b'{}')
        response.json.return_value={'contact':{'id':'ref-contact'}}
        with patch.dict(os.environ,{'GHL_ACCESS_TOKEN':'test','GHL_LOCATION_ID':'test'}), \
             patch.object(f,'get_ghl_custom_field_map',return_value={}), \
             patch.object(f.requests,'post',return_value=response) as post, \
             patch.object(f,'retrigger_ghl_tag',return_value={'success':trigger_success}) as trigger, \
             patch.object(f.time,'sleep'):
            result=f.notify_referrer_via_ghl(ref,'Client Test')
        return result,post,trigger

    def test_email_triggers_without_phone(self):
        result,post,trigger=self.notify()
        self.assertTrue(result['success'])
        self.assertEqual(trigger.call_count,2)
        self.assertNotIn('phone',post.call_args.kwargs['json'])

    def test_normalized_referrer_phone(self):
        result,post,_=self.notify('+61 0422333444')
        self.assertTrue(result['success'])
        self.assertEqual(post.call_args.kwargs['json']['phone'],'+61422333444')

    def test_invalid_phone_does_not_block_email(self):
        result,post,_=self.notify('invalid')
        self.assertTrue(result['success'])
        self.assertNotIn('phone',post.call_args.kwargs['json'])

    def test_failed_email_trigger_is_not_reported_as_success(self):
        result,_,_=self.notify(trigger_success=False)
        self.assertFalse(result['success'])
        self.assertTrue(result['contactSynced'])
