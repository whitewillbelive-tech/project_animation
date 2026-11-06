import tempfile
import unittest
from pathlib import Path
from app.store import Store, ApiError
from app.migration import preview, import_file


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'state.sqlite3')
        self.project = self.store.init_owner('검증 소유자', 'test-only-long-password')
        self.pid = self.project['id']
        self.payload = dict(format='markdown', filename='원본.md',
                            source_url='https://app.notion.com/p/3f3e38d0824b817086c4ffa35a1dd7de',
                            content='# 테스트 문서\n원문 해석: 미정\n<script>alert(1)</script>')

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def apply(self, payload=None):
        p = dict(payload or self.payload)
        p['fingerprint'] = preview(self.store, self.pid, p)['fingerprint']
        return import_file(self.store, self.pid, p, '검증 소유자')

    def test_reimport_is_idempotent_and_preserves_provenance(self):
        self.assertEqual(self.apply()['created'], 1)
        self.assertEqual(self.apply()['created'], 0)
        records = self.store.state(self.pid)['records']
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['source']['id'], '3f3e38d0824b817086c4ffa35a1dd7de')
        self.assertTrue(records[0]['source']['read_at'].endswith('Z'))
        self.assertFalse(records[0]['source']['modified_at'])
        self.assertIn('<script>', records[0]['content'])  # Stored as text; UI escapes.

    def test_local_edit_or_changed_source_never_silently_overwrites(self):
        self.apply()
        r = self.store.state(self.pid)['records'][0]
        self.store.update_record(self.pid, r['id'], {'revision':r['revision'], 'content':'사용자 편집'}, '검증 소유자')
        self.assertEqual(preview(self.store, self.pid, self.payload)['status'], 'conflict')
        with self.assertRaises(ApiError): self.apply()
        self.assertEqual(self.store.get_record(self.pid, r['id'])['content'], '사용자 편집')

    def test_changed_preview_rejected(self):
        p = dict(self.payload, fingerprint=preview(self.store, self.pid, self.payload)['fingerprint'])
        p['content'] += 'changed'
        with self.assertRaises(ApiError) as cm: import_file(self.store,self.pid,p,'소유자')
        self.assertEqual(cm.exception.status,409)
        self.assertEqual(len(self.store.state(self.pid)['records']),0)

    def test_csv_types_source_and_duplicates(self):
        p = dict(format='csv',filename='자료.csv',source_url='',
                 content='원본 ID,제목,유형,카테고리,내용\nref1,자료,reference,F,검토 전\nshot1,숏,shot,C,미정')
        self.assertEqual(self.apply(p)['created'],2)
        self.assertEqual({r['type'] for r in self.store.state(self.pid)['records']},{'reference','shot'})
        p['content'] += '\nref1,중복,reference,F,미정'
        with self.assertRaises(ApiError): preview(self.store,self.pid,p)

    def test_invalid_files(self):
        for change in ({'format':'zip'}, {'filename':'../escape.md'}, {'source_url':'javascript:alert(1)'},
                       {'content':'x'*(2*1024*1024+1)}, {'source_modified_at':'2026-11-06'}):
            with self.subTest(change=list(change)), self.assertRaises(ApiError):
                preview(self.store,self.pid,{**self.payload,**change})


if __name__ == '__main__':
    unittest.main()
