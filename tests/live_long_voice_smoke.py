"""Opt-in real offline Silero -> Whisper; no recording, playback or execution."""
import time
import numpy as np
from assistant.voice import VoiceSession
from assistant.worker import WakeGate

text = ('Цитадель, сохрани заметку о подготовке рабочего места. '
        'Сначала нужно проверить подключение монитора и клавиатуры, затем открыть папку с документами проекта. '
        'В этой папке находится инструкция по настройке сервера и список задач на следующую неделю. '
        'Отдельно запиши, что резервное копирование следует проверить после завершения загрузки архива. '
        'Не удаляй исходные документы и не меняй системные настройки без моего разрешения. '
        'В конце заметки обязательно добавь контрольные слова: северный маяк.')
session = VoiceSession(allow_download=False,allow_sapi_fallback=False)
started = time.monotonic()
model, torch = session._load_tts()
speaker, rate = session._tts_settings(model)
pieces = []
with torch.inference_mode():
    for part in session._speech_chunks(text):
        samples = model.apply_tts(text=part,speaker=speaker,sample_rate=rate,put_accent=True,put_yo=True)
        pieces.extend([samples.detach().cpu().numpy(),np.zeros(round(rate*.9),dtype=np.float32)])
audio = np.concatenate(pieces)
duration = len(audio)/rate
assert duration>18,duration
audio = np.interp(np.arange(round(duration*16000))*rate/16000,np.arange(len(audio)),audio)
raw = (np.clip(audio,-1,1)*32767).astype('<i2').tobytes()
recognized = session.transcribe_audio(raw)
command, _ = WakeGate('wake').accept(recognized or '')
print(f'Duration: {duration:.2f}s; elapsed: {time.monotonic()-started:.2f}s',flush=True)
print(recognized,flush=True)
assert command,'Wake address was lost'
assert 'северный маяк' in command.lower(),'The end of the long utterance was lost'
assert 'резервн' in command.lower(),'The middle of the utterance was lost'
print('PASS: offline real STT retains wake word, middle and final words beyond 18 seconds with 0.9s pauses')
