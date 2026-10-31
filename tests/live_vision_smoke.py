"""Opt-in local multimodal model reads an owned synthetic image, no screen capture."""
import base64
import io
import os
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
from assistant.ollama_client import OllamaClient
from assistant.config import OLLAMA_URL

model=os.environ.get('CITADEL_TEST_VISION_MODEL','qwen3.5:4b')
client=OllamaClient(OLLAMA_URL,model)
assert client.supports_vision,'Model does not report vision capability'
picture=Image.new('RGB',(480,180),'white')
font=ImageFont.truetype(str(Path(os.environ.get('SystemRoot','C:/Windows'))/'Fonts/arialbd.ttf'),92)
ImageDraw.Draw(picture).text((45,25),'42',font=font,fill='black')
buffer=io.BytesIO(); picture.save(buffer,format='PNG')
client.num_predict=50
result=client.chat([{'role':'user','content':'Какое число написано на изображении? Ответь только этим числом.'}],[],images=[base64.b64encode(buffer.getvalue()).decode()],timeout=120)
answer=result['message'].get('content','').strip()
assert '42' in answer,answer
print('PASS: actual local vision model read 42 from generated test pixels; no user screen/files captured')
