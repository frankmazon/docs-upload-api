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

class AdminPrivacyTests(unittest.TestCase):
    def setup_handler(self, name='admin_privacy_document'):
        ns = functions(name)
        conn, cursor = Mock(), Mock()
        conn.cursor.return_value = cursor
        ns.update(read_chat_session=Mock(return_value={'role':'Admin','id':9}),
                  chat_response=lambda body,status=200:(status,body),
                  get_sql_connection=Mock(return_value=conn),
                  ensure_privacy_documents_table=Mock(), close_sql_resources=Mock(),
                  privacy_submission=Mock(return_value=None))
        return ns, conn, cursor

    def test_admin_document_rejects_client_and_anonymous(self):
        ns, conn, cursor = self.setup_handler()
        for session in (None, {'role':'Client','id':1}, {'role':'Referrer','id':1}):
            ns['read_chat_session'].return_value = session
            self.assertEqual(ns['admin_privacy_document'](types.SimpleNamespace(method='GET',params={}))[0],401)
        ns['get_sql_connection'].assert_not_called()

    def test_saved_document_is_exact_snapshot_not_current_client_data(self):
        import json
        ns, conn, cursor = self.setup_handler()
        saved={'signedAt':'2026-10-10','submission':{'borrowers':[{'name':'Original name'}]},'signatures':{'borrower':{'image':'saved-image'}}}
        cursor.fetchone.return_value=(json.dumps(saved),)
        self.assertEqual(ns['admin_privacy_document'](types.SimpleNamespace(method='GET',params={'clientId':'3'})), (200,saved))
        ns['privacy_submission'].assert_not_called()
        ns['close_sql_resources'].assert_called_once_with(cursor,conn)

    def test_status_list_excludes_signature_images(self):
        ns, conn, cursor = self.setup_handler()
        cursor.fetchall.return_value=[types.SimpleNamespace(ClientId=3,SignedAt=datetime(2026,10,10))]
        status, data=ns['admin_privacy_document'](types.SimpleNamespace(method='GET',params={}))
        self.assertEqual(status,200)
        self.assertEqual(set(data['documents'][0]),{'clientId','signedAt'})

    def test_missing_and_invalid_client(self):
        ns, conn, cursor = self.setup_handler()
        cursor.fetchone.return_value=None
        self.assertEqual(ns['admin_privacy_document'](types.SimpleNamespace(method='GET',params={'clientId':'3'}))[0],404)
        self.assertEqual(ns['admin_privacy_document'](types.SimpleNamespace(method='GET',params={'clientId':'-1'}))[0],400)

    def test_notification_is_atomic_and_not_repeated_on_reopen(self):
        import json, uuid
        from datetime import timezone
        ns, conn, cursor = self.setup_handler('privacy_document')
        ns.update(datetime=datetime,timezone=timezone,uuid=uuid,
                  create_admin_notification=Mock(),privacy_signatures=Mock(return_value={'borrower':{'image':'saved'}}))
        ns['read_chat_session'].return_value={'role':'Client','id':3}
        document={'revision':'r','templateVersion':1,'submission':{'uniqueId':'TEST','borrowers':[{'id':'borrower','name':'Test Client'}]}}
        ns['privacy_submission'].return_value=document
        cursor.fetchone.side_effect=[(3,),None]
        req=types.SimpleNamespace(method='POST',get_json=lambda:{'revision':'r','templateVersion':1})
        status,saved=ns['privacy_document'](req)
        self.assertEqual(status,200)
        self.assertEqual(ns['create_admin_notification'].call_args.kwargs['redirect_to'],'/dashboard/privacy-authority/3')
        self.assertEqual(conn.commit.call_count,2)
        cursor.fetchone.side_effect=[(3,),(json.dumps(saved),)]
        self.assertEqual(ns['privacy_document'](req)[0],200)
        ns['create_admin_notification'].assert_called_once()
