"""Explicit voice settings preparation, separate from the offline voice host."""
import json
from pathlib import Path
import sys
import tempfile

from .paths import DATA_ROOT


def save_updates(updates):
    path = DATA_ROOT/'assistant.local.json'
    config = json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {}
    config.update(updates)
    path.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=path.parent,suffix='.json.part',delete=False) as output:
        pending = Path(output.name)
        json.dump(config,output,ensure_ascii=False,indent=2)
    try:
        pending.replace(path)
    finally:
        pending.unlink(missing_ok=True)


def prepare_profile(profile,emit):
    if profile not in {'jarvis','neutral'}:
        raise ValueError('Профиль jarvis или neutral')
    from .voice import VoiceSession
    from .voice_assets import _validate_audio
    speaker = 'eugene' if profile=='jarvis' else 'aidar'
    emit('status',{'message':'Подготовка мужского голоса Silero. Профиль «Джарвис» использует eugene, это не копия голоса актёра.'})
    session = VoiceSession(tts_engine='silero',tts_speaker=speaker,allow_download=True,allow_sapi_fallback=False,
                           on_status=lambda message:emit('status',{'message':message}))
    model,torch = session._load_tts()
    if speaker not in model.speakers:
        raise ValueError('Выбранный голос отсутствует в этой модели Silero')
    speaker,rate = session._tts_settings(model)
    with torch.inference_mode():
        audio = model.apply_tts(text='Принято. Напоминание установлено.',speaker=speaker,sample_rate=rate,put_accent=True,put_yo=True)
    _validate_audio(audio,'Silero voice profile')
    save_updates({'tts_engine':'silero','tts_speaker':speaker,'voice_profile':profile})
    emit('ready',{'message':'Голос проверен и сохранён. Перезапустите Цитадель для применения.'})


def prepare_stt(model: str, emit) -> None:
    if model not in {'base','small'}:
        raise ValueError('Выберите base или small')
    from .voice import VoiceSession
    emit('status', {'message': f'Подготовка Whisper {model}. При отсутствии модели она будет скачана; это может занять несколько минут.'})
    session = VoiceSession(stt_model=model,allow_download=True,on_status=lambda message: emit('status',{'message':message}))
    session.prepare(speech=False)
    session.warmup(speech=False)
    # Change the setting only after real local inference succeeds. Re-read the
    # file so other settings changed during a long download are preserved.
    path = DATA_ROOT/'assistant.local.json'
    config = json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {}
    config['stt_model'] = model
    config.setdefault('voice_beam_size',3)
    path.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=path.parent,suffix='.json.part',delete=False) as output:
        pending = Path(output.name)
        json.dump(config,output,ensure_ascii=False,indent=2)
    try:
        pending.replace(path)
    finally:
        pending.unlink(missing_ok=True)
    emit('ready',{'message':f'Whisper {model} готов. Выбор применится после перезапуска Цитадели.'})


def main() -> int:
    output = sys.stdout
    sys.stdout = sys.stderr
    def emit(event,payload):
        output.write(json.dumps({'event':event,'payload':payload},ensure_ascii=False)+'\n')
        output.flush()
    try:
        payload = json.loads(sys.stdin.readline())
        if payload.get('agent_model'):
            from .model_setup import prepare_agent
            prepare_agent(payload['agent_model'],emit)
        elif payload.get('profile'):
            prepare_profile(payload['profile'],emit)
        else:
            prepare_stt(payload['model'],emit)
        return 0
    except Exception as error:
        emit('error',{'message':str(error)})
        return 1


if __name__=='__main__':
    raise SystemExit(main())
