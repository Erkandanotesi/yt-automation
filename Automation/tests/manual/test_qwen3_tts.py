import sys
import time
from pathlib import Path
import soundfile as sf
import torch

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

def test_qwen_custom_voice():
    print("=" * 65)
    print("Qwen3-TTS 1.7B-CustomVoice Local Test (GPU Inference)")
    print("=" * 65)

    device = "cuda:0" if torch.cuda.is_available() else "cpu"
    gpu_name = torch.cuda.get_device_name(0) if torch.cuda.is_available() else "CPU"
    print(f"Target Device: {device} ({gpu_name})")

    from qwen_tts import Qwen3TTSModel

    import sys as _sys

_automation_root = Path(__file__).resolve().parents[2]
if str(_automation_root / "src") not in _sys.path:
    _sys.path.insert(0, str(_automation_root / "src"))
from file_locations import MODELS_ROOT

model_dir = MODELS_ROOT / "Qwen3-TTS-12Hz-1.7B-CustomVoice"
    print(f"\n[1/3] Loading local model from: {model_dir} ...")
    start_load = time.time()
    
    dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
    print(f"Data precision: {dtype}")

    model = Qwen3TTSModel.from_pretrained(
        str(model_dir),
        device_map=device,
        torch_dtype=dtype,
    )
    load_time = time.time() - start_load
    print(f"Model loaded into GPU VRAM in {load_time:.2f} seconds.")

    speakers = model.get_supported_speakers()
    print(f"Supported Speakers: {speakers}")

    languages = model.get_supported_languages()
    print(f"Supported Languages: {languages}")

    test_text = (
        "Welcome back to Tech News! Today, open source speech synthesis reaches a new milestone "
        "with ultra-fast low-latency streaming and expressive voice controls running right on your GPU."
    )
    speaker = "ryan" if speakers and "ryan" in speakers else (speakers[0] if speakers else "ryan")
    language = "English"
    instruct = "Speak with clear confidence and an energetic, engaging tech podcast tone."

    print(f"\n[2/3] Synthesizing speech ...")
    print(f"Text: \"{test_text}\"")
    print(f"Speaker: {speaker} | Language: {language}")
    print(f"Instruction: {instruct}")

    start_gen = time.time()
    wavs, sr = model.generate_custom_voice(
        text=test_text,
        language=language,
        speaker=speaker,
        instruct=instruct,
    )
    gen_time = time.time() - start_gen

    audio_data = wavs[0]
    audio_duration = len(audio_data) / sr
    rtf = gen_time / audio_duration if audio_duration > 0 else 0

    output_dir = Path(__file__).resolve().parent
    output_path = output_dir / "qwen3_test_customvoice_1.7b.wav"
    sf.write(str(output_path), audio_data, sr)

    print(f"\n[3/3] Synthesis Complete!")
    print(f"Output File: {output_path}")
    print(f"Sample Rate: {sr} Hz")
    print(f"Audio Duration: {audio_duration:.2f} seconds")
    print(f"Inference Time: {gen_time:.2f} seconds")
    print(f"Real-Time Factor (RTF): {rtf:.3f}x")
    if rtf > 0:
        print(f"Generation Speed: {audio_duration / gen_time:.2f}x real-time ({(audio_duration / gen_time):.1f} seconds of audio generated per second)")
    print("=" * 65)

if __name__ == "__main__":
    test_qwen_custom_voice()
