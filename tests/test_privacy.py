import base64
import hashlib
import types
import unittest
from unittest.mock import Mock
from datetime import datetime
from test_reliability import functions

class PrivacyTests(unittest.TestCase):
    def submission(self, flag, co):
        ns = functions('privacy_submission'); ns['hashlib'] = hashlib
        cursor = Mock(); cursor.fetchone.return_value = types.SimpleNamespace(UniqueId='TEST',FirstName='Alex',MiddleName=None,LastName='Test',SubmittedAt=datetime(2026,9,20),WithBorrowersGuarantors=flag)
        cursor.fetchall.return_value = co
        return ns, cursor

    def test_no_flag_suppresses_stale_coborrowers(self):
        ns, cursor = self.submission('No', [object()])
        result = ns['privacy_submission'](cursor, 1)
        self.assertEqual(len(result['submission']['borrowers']),1)
        self.assertEqual(result['submission']['submittedAt'],'2026-09-20')
        cursor.fetchall.assert_not_called()

    def test_yes_uses_saved_names_and_ids(self):
        ns, cursor = self.submission('Yes', [types.SimpleNamespace(Id=7,FirstName='Jamie',MiddleName=None,LastName='Test')])
        result = ns['privacy_submission'](cursor,1)
        self.assertEqual(result['submission']['borrowers'][1], {'id':'co-7','name':'Jamie Test'})

    def test_yes_without_details_blocks_incomplete_document(self):
        ns, cursor = self.submission('Yes',[])
        with self.assertRaises(ValueError): ns['privacy_submission'](cursor,1)

    def test_signatures_require_all_borrowers_consent_and_png(self):
        ns=functions('privacy_signatures');ns['base64']=base64
        borrowers=[{'id':'borrower'}, {'id':'co-7'}]
        for data in ({'signatures':{}}, {'signatures':{'borrower':{'consent':True},'co-7':{'consent':False}}}, {'signatures':{'borrower':{'consent':True,'image':'data:image/png;base64,AAAA'},'co-7':{'consent':True}}}):
            with self.assertRaises(ValueError): ns['privacy_signatures'](data,borrowers,'now')

    def test_unauthenticated_and_admin_cannot_sign(self):
        ns=functions('privacy_document'); ns['chat_response']=lambda body,status=200:(status,body)
        ns['get_sql_connection']=Mock()
        for session in (None, {'role':'Admin','id':1}):
            ns['read_chat_session']=lambda req:session
            self.assertEqual(ns['privacy_document'](types.SimpleNamespace(method='POST'))[0],401)
        ns['get_sql_connection'].assert_not_called()
