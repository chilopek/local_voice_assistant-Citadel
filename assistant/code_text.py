"""Validate syntax without executing; repair only provable transport escaping."""
import ast
import re


def python_source(text: str) -> tuple[str,bool]:
    try:
        ast.parse(text)
        return text,False
    except SyntaxError as original:
        if '\\n' not in text or text.count('\n')>1:
            raise ValueError(f'Синтаксис Python: {original.msg}, строка {original.lineno}. Исправь код до записи.') from original
        protected=[]
        # tokenize can stop before later string tokens on invalid transport
        # text. Scan quoted spans first; refuse complex f-string repair.
        if re.search(r'\bf[\'"]',text,re.I):
            raise ValueError('Передай Python-код с настоящими переводами строк; автоматическая правка f-строк отключена.') from original
        index=0
        while index<len(text):
            if text[index] in {'"',"'"}:
                start=index
                quote=text[index]*3 if text.startswith(text[index]*3,index) else text[index]
                index+=len(quote)
                while index<len(text):
                    if text[index]=='\\':
                        index+=2
                    elif text.startswith(quote,index):
                        index+=len(quote)
                        protected.append((start,index))
                        break
                    else:
                        index+=1
                else:
                    raise ValueError('Незакрытая строка Python; исправь исходный код.') from original
            elif text[index]=='#':
                end=text.find('\n',index)
                protected.append((index,len(text) if end<0 else end))
                index=len(text) if end<0 else end+1
            else:
                index+=1
        def replace(match):
            if any(start<=match.start()<end for start,end in protected):
                return match[0]
            return '\t' if match[0]=='\\t' else '\n'
        candidate=re.sub(r'\\r\\n|\\n|\\t',replace,text)
        try:
            ast.parse(candidate)
        except SyntaxError:
            raise ValueError(f'Синтаксис Python: {original.msg}, строка {original.lineno}. Передай настоящие переводы строк вместо двойного экранирования.') from original
        return candidate,True
