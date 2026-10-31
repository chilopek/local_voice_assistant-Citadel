import hashlib
from pathlib import Path
import tempfile
import unittest
import zipfile
from unittest.mock import patch
from assistant import tools, file_tools


class FileToolTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        try:
            from tests.temp_support import cleanup_directory
        except ModuleNotFoundError:
            from temp_support import cleanup_directory
        self.addCleanup(cleanup_directory,self.temp)
        self.root=Path(self.temp.name).resolve()
        for name in ('assistant.tools.WORKSPACE','assistant.file_tools.DATA_ROOT'):
            setting=patch(name,self.root); setting.start(); self.addCleanup(setting.stop)

    def test_exact_edit_is_reversible_preserves_encoding_and_rejects_stale_read(self):
        target=self.root/'заметка.txt'
        original='Привет\r\nСтарый текст\r\n'.encode('cp1251')
        target.write_bytes(original)
        current=tools.read_file(str(target))
        self.assertEqual(current['encoding'],'cp1251')
        self.assertIn('Старый текст',current['content'])
        args={'path':str(target),'old_text':'Старый текст','new_text':'Новый текст','expected_sha256':current['sha256']}
        with self.assertRaises(PermissionError): tools.execute('edit_file',args)
        changed=tools.execute('edit_file',args,confirmed=True)
        self.assertTrue(changed['verified'])
        self.assertIn(b'\r\n',target.read_bytes())
        with self.assertRaises(ValueError): tools.execute('edit_file',args,confirmed=True)
        restored=tools.execute('restore_file_revision',{'revision_id':changed['revision_id'],'expected_sha256':changed['sha256']},confirmed=True)
        self.assertTrue(restored['verified']); self.assertEqual(target.read_bytes(),original)

    def test_edit_ambiguous_or_invalid_python_never_touches_original(self):
        target=self.root/'code.py'; target.write_text('x = 1\nx = 1\n')
        current=tools.read_file(str(target))
        args={'path':str(target),'old_text':'x = 1','new_text':'x = 2','expected_sha256':current['sha256']}
        with self.assertRaises(ValueError): tools.execute('edit_file',args,confirmed=True)
        args.update(old_text=current['content'],new_text='def bad(:')
        with self.assertRaises(SyntaxError): tools.execute('edit_file',args,confirmed=True)
        self.assertEqual(target.read_bytes().decode('utf-8'),current['content'])

    def test_restore_does_not_overwrite_new_external_changes(self):
        target=self.root/'a.txt'; target.write_text('before')
        result=tools.execute('edit_file',{'path':str(target),'old_text':'before','new_text':'after','expected_sha256':hashlib.sha256(target.read_bytes()).hexdigest()},confirmed=True)
        target.write_text('someone else changed it')
        with self.assertRaises(ValueError): tools.execute('restore_file_revision',{'revision_id':result['revision_id'],'expected_sha256':result['sha256']},confirmed=True)
        self.assertEqual(target.read_text(),'someone else changed it')

    def test_content_search_finds_unicode_with_line_and_skips_binary_and_service_dirs(self):
        (self.root/'nested').mkdir(); (self.root/'nested'/'a.py').write_text('first\nсекретный алгоритм\n',encoding='utf-8')
        (self.root/'.git').mkdir(); (self.root/'.git'/'b.txt').write_text('секретный',encoding='utf-8')
        (self.root/'binary.txt').write_bytes(b'\0secret')
        result=file_tools.search_file_contents('секретный',str(self.root))
        self.assertEqual(len(result['matches']),1)
        self.assertEqual(result['matches'][0]['line'],2)
        self.assertFalse(result['truncated'])

    def make_zip(self,name,parts):
        path=self.root/name
        with zipfile.ZipFile(path,'w') as archive:
            for key,value in parts.items(): archive.writestr(key,value)
        return str(path)

    def test_docx_and_pptx_extract_text_without_office_or_macros(self):
        doc=self.make_zip('a.docx',{'word/document.xml':'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:p><w:r><w:t>Привет мир</w:t></w:r></w:p></w:document>'})
        self.assertIn('Привет мир',file_tools.read_document(doc)['content'])
        ppt=self.make_zip('a.pptx',{'ppt/slides/slide1.xml':'<s xmlns:a="x"><a:t>Слайд один</a:t></s>','ppt/slides/slide2.xml':'<s xmlns:a="x"><a:t>Слайд два</a:t></s>'})
        text=file_tools.read_document(ppt)['content']
        self.assertLess(text.index('один'),text.index('два'))

    def test_xlsx_shared_inline_strings_and_cached_formula_values(self):
        ns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
        path=self.make_zip('a.xlsx',{'xl/sharedStrings.xml':f'<sst xmlns="{ns}"><si><t>Название</t></si></sst>',
            'xl/worksheets/sheet1.xml':f'<worksheet xmlns="{ns}"><sheetData><row><c r="A1" t="s"><v>0</v></c><c r="B1"><f>1+1</f><v>2</v></c><c r="C1" t="inlineStr"><is><t>Текст</t></is></c></row></sheetData></worksheet>'})
        text=file_tools.read_document(path)['content']
        for expected in ('A1=Название','B1=2','1+1','C1=Текст'): self.assertIn(expected,text)

    def test_document_pagination_does_not_drop_text(self):
        body=''.join(str(i%10) for i in range(600))
        path=self.make_zip('long.docx',{'word/document.xml':f'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:p><w:t>{body}</w:t></w:p></w:document>'})
        text=''; start=0
        for _ in range(10):
            part=file_tools.read_document(path,start=start,max_chars=100)
            text+=part['content']
            if part['next_start'] is None: break
            self.assertGreater(part['next_start'],start)
            start=part['next_start']
        self.assertEqual(text,body+'\n')

    def test_pdf_has_actual_text_layer_and_blank_scan_is_not_invented(self):
        from pypdf import PdfWriter
        from pypdf.generic import DictionaryObject,NameObject,DecodedStreamObject
        writer=PdfWriter(); page=writer.add_blank_page(width=300,height=300)
        font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
        page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):writer._add_object(font)})})
        stream=DecodedStreamObject(); stream.set_data(b'BT /F1 12 Tf 10 100 Td (Citadel test text) Tj ET')
        page[NameObject('/Contents')]=writer._add_object(stream)
        path=self.root/'a.pdf'; writer.write(path)
        self.assertIn('Citadel test text',file_tools.read_document(str(path))['content'])
        writer=PdfWriter(); writer.add_blank_page(width=300,height=300); writer.write(path)
        result=file_tools.read_document(str(path))
        self.assertNotIn('Citadel test text',result['content'])
        self.assertIn('сканы',result['note'])

    def test_reading_continues_from_actual_cursor_without_another_model_call(self):
        import json
        from unittest.mock import Mock
        from assistant import app
        from assistant.memory import Memory
        target=self.root/'long.txt'
        target.write_text('first part.'*1000+'Последний абзац.',encoding='utf-8')
        value=tools.read_file(str(target),max_chars=8000)
        messages=[{'role':'user','content':'Прочитай файл'}, {'role':'tool','tool_name':'read_file','content':json.dumps({'ok':True,'result':value})}]
        events=[]; model=Mock()
        with patch.object(app,'PROJECT_ROOT',self.root),patch.object(app,'WORKSPACE',self.root):
            answer=app.run_turn(model,Memory(self.root/'memory.sqlite'),messages,'прочитай дальше',on_event=events.append)
        model.chat.assert_not_called()
        self.assertIn('Последний абзац.',answer)
        done=next(event for event in events if event['type']=='done')
        self.assertIn('Последний абзац.',done['speech']['text'])
