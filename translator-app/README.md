# 🎬 Video Translator — ဗီဒီယို မြန်မာဘာသာပြန် (MVP)

ဗီဒီယိုလင့်ခ် တစ်ခုပဲ ထည့်လိုက်ရင် အစအဆုံး အလိုအလျောက်:

1. **Download** — yt-dlp နဲ့ ဗီဒီယို ဒေါင်းလုပ်
2. **Transcribe** — faster-whisper (small model) နဲ့ စကားသံကို အချိန်အပိုင်းလိုက် စာသားထုတ်
3. **Translate** — အပိုင်းတိုင်းကို မြန်မာလို ဘာသာပြန် (အခမဲ့၊ API key မလို)
4. **ထုတ်ပေးပုံ ရွေးနိုင်**:
   - `subs` စာတန်းထိုး — မြန်မာစာတန်း ဗီဒီယိုထဲ မီးရှို့ထည့် (burn-in)၊ မူရင်းအသံ၊ မူရင်းအချိန်
   - `dub` အသံထွက် — မြန်မာအသံနဲ့ အစားထိုး (စကားမရှိတဲ့ နေရာတွေမှာ မူရင်းအသံ အတိုးအကျယ် လျှော့ထား)
   - `both` နှစ်မျိုးလုံး — အသံထွက် + စာတန်းထိုး (အချိန်အသစ် ကိုက်ညှိပြီး)
5. **⏱ အချိန်ကိုက် Sync (core feature)** — အပိုင်းတိုင်းအတွက် မြန်မာအသံကို သူ့အချိန်ပြတင်းပေါက်ထဲ ဝင်အောင်:
   - တိုနေရင် — အသံအဆုံးမှာ တိတ်ဆိတ်ခန်း ဖြည့်တယ်
   - ရှည်နေရင် — ffmpeg `atempo` နဲ့ အသံကို အမြန်တင်တယ် (အများဆုံး **1.30×** အထိပဲ)
   - 1.30× နဲ့တောင် မလုံရင် — အဲဒီအပိုင်းရဲ့ **နောက်ဆုံးဖရိမ်ကို ရပ်ထားပြီး ဗီဒီယိုကို လိုသလောက် ဆန့်ထုတ်**တယ် (freeze-frame extension) — စကားသံ ဖြတ်မချဘူး
   - စာတန်းအချိန်တွေကိုလည်း ဆန့်ထားတဲ့ အချိန်အသစ်နဲ့ ပြန်ညှိပေးတယ်
6. **Output** — ဒေါင်းလုပ် လုပ်နိုင်တဲ့ `.mp4` တစ်ခုတည်း

---

## Run instructions

လိုအပ်ချက်: Python 3.12, `ffmpeg`/`ffprobe` (PATH ထဲမှာ ရှိရမယ်)

```bash
cd ~/workspace/video-translator

# first time only — venv + dependencies (already done in this workspace)
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# start the server (default port 8611, override with PORT=xxxx)
./run.sh
# or: .venv/bin/python app.py
```

ဖုန်း/ဘရောက်ဇာကနေ ဖွင့်ပါ: **http://localhost:8611/** (တူညီတဲ့ network ထဲကဆို `http://<computer-ip>:8611/`)

UI မှာ: လင့်ခက်ထည့် → ပုံစံရွေး (စာတန်းထိုး / အသံထွက် / နှစ်မျိုးလုံး) → အသံရွေး → "စတင်လုပ်မယ်" → အဆင့်လိုက် progress ကြည့် → ပြီးရင် preview + ဒေါင်းလုပ် ခလုတ် ပေါ်မယ်။

### Whisper model

`models/faster-whisper-small/` ထဲမှာ model အပြည့်အစုံ ထည့်ထားပြီးသား (483MB) — ပြန် download လုပ်စရာ မလိုဘူး။ အဲဒီဖိုလ်ဒါ မရှိရင် faster-whisper က HuggingFace ကနေ ပထမဆုံး run မှာ auto-download လုပ်မယ်။ Model အရွယ်ကို `WHISPER_MODEL=tiny|base|small|medium` env နဲ့ ပြောင်းနိုင်တယ် (မြန်တာ/မှန်တာ အပြန်အလှန်).

### Myanmar font (စာတန်းအတွက်)

**Noto Sans Myanmar** ကို `assets/fonts/` ထဲမှာ bundle ထည့်ထားပြီး ffmpeg `subtitles` filter ကို `fontsdir` နဲ့ ညွှန်ထားတယ် — ဒါကြောင့် မြန်မာစာ tofu (▯▯▯) မဖြစ်ဘဲ အမှန် glyph တွေ ပေါ်တယ် (verification frame မှာ စစ်ပြီးသား)။ System မှာ font ထပ်ထည့်စရာ မလိုဘူး။

### Voices (အသံ)

edge-tts my-MM voices (list စစ်ပြီး):
- `my-MM-NilarNeural` — အမျိုးသမီး
- `my-MM-ThihaNeural` — အမျိုးသား

---

## Pipeline modules (CLI နဲ့ တစ်ခုချင်း စမ်းနိုင်တယ်)

| Module | လုပ်တာ | CLI |
|---|---|---|
| `pipeline/download.py` | yt-dlp download (+ captions best-effort) | `python -m pipeline.download <url> <workdir>` |
| `pipeline/transcribe.py` | faster-whisper segments (သို့ `--captions` ဆိုရင် ရှိပြီးသား caption fast-path) | `python -m pipeline.transcribe <src.mp4> <workdir>` |
| `pipeline/translate_segments.py` | မြန်မာ ဘာသာပြန် (provider chain ↓) | `python -m pipeline.translate_segments <workdir>` |
| `pipeline/tts.py` | အပိုင်းလိုက် မြန်မာအသံ (provider chain ↓) | `python -m pipeline.tts <workdir> [voice]` |
| `pipeline/retime.py` | atempo + freeze-frame sync, `retime_plan.json` ထုတ် | `python -m pipeline.retime <workdir>` |
| `pipeline/burn.py` | စာတန်း burn-in (Noto Sans Myanmar) | `python -m pipeline.burn <in.mp4> <subs.srt> <out.mp4>` |
| `pipeline/run_job.py` | အားလုံးကို server က ခေါ်တဲ့ orchestrator | — |

Job အလုပ် folder တွေ: `jobs/<job-id>/` (source, segments JSON, SRT, chunks, job.json, output.mp4)။

### Free-provider chains (API key မလို) — ဘာကြောင့် chain လုပ်ထားလဲ

Network အပေါ် မူတည်ပြီး အခမဲ့ endpoint တစ်ခုဟာ တစ်ခါတစ်ရံ ပိတ်နိုင်လို့:

- **Translate**: ① deep-translator `GoogleTranslator` → ② `translators` **Bing** → ③ မူရင်းစာ (cue မပျောက်အောင်)
- **TTS**: ① **edge-tts** (Nilar/Thiha neural) → ② **Google Translate TTS classic endpoint** (`translate_tts?client=tw-ob`, `lang='my'`) → ③ **gTTS** (`lang='my'`၊ အသံတစ်မျိုးတည်း၊ tld လှည့် + backoff retry ပါ)

ဒီ sandbox မှာ တွေ့ခဲ့ရတဲ့ အမှန်အခြေအနေ (2026-11-07): sandbox ရဲ့ shared egress IP ကို Google Translate က "sorry/too many requests" နဲ့ ပိတ်ထားပြီး MyMemory free quota လည်း IP အလိုက် ကုန်နေလို့ **Bing** က ပြန်ပေးခဲ့တယ်။ edge-tts ရဲ့ websocket handshake ကိုလည်း sandbox proxy က ဖြတ်လို့ ဒီမှာတော့ **gTTS** က ပြောပေးခဲ့တယ်။ သာမန် အိမ်/ဖုန်း network မှာဆိုရင် ပထမဆုံး provider (Google / edge-tts) တွေ ပုံမှန် အလုပ်လုပ်နိုင်ချ များတယ်။ ဘယ် provider သုံးခဲ့လဲဆိုတာ segment တိုင်းရဲ
...[truncated 2705 chars]
---

## Demo update (2026-11-07) — Editor: Logo + Blur + Cinematic subs + Font picker

Link ထည့်ပြီး **"🔍 Preview ယူမယ်"** နှိပ်ရင် ဗီဒီယိုကို တစ်ခါ download လုပ်ပြီး editor screen ပွင့်တယ် (အချိန် slider နဲ့ ဖရိမ် ရွေးကြည့်နိုင်). အဲဒီ screen တစ်ခုတည်းပေါ်မှာ:

### 🏷️ Logo / watermark
- Logo ပုံ upload (PNG နောက်ခံပွင့် အကောင်းဆုံး) → preview ပေါ်မှာ **လက်/မောက်စ်နဲ့ ဆွဲပြီး ကြိုက်တဲ့နေရာ** ချနိုင် (ထောင့် ၄ ထောင့် shortcut ခလုတ်လည်း ပါ)
- Slider တွေ: **အရွယ်** (ဗီဒီယို အကျယ်ရဲ့ %), **ဖျော့မှု/opacity** (10–100%), **ကာလာ ဖျော့မှု** (saturation 0–100%)
- နေရာကို normalized 0–1 နဲ့ သိမ်းလို့ ဗီဒီယို resolution ဘယ်လောက်ဖြစ်ဖြစ် အချိုးတူကျ
- ffmpeg: logo ကို scale → hue saturation → alpha (colorchannelmixer) → overlay (ဗီဒီယို တစ်လျှောက်လုံး)

### 🌫️ မူရင်းစာ ဝါးရန် (blur)
- Preview ဖရိမ်ပေါ်မှာ ဝါးချင်တဲ့နေရာကို **လက်နဲ့ ဆွဲပြီး အကွက်ဆွဲ** (အကွက်ဟောင်းထဲ ဆွဲရင် ရွေ့, နေရာသစ်မှာ ဆွဲရင် အကွက်အသစ်) — အကွက် **တစ်ကွက်တည်း**, ဗီဒီယို တစ်လျှောက်လုံး သက်ရောက်
- **ဝါးအား** slider (gblur sigma 5–60)
- ffmpeg: crop → gblur → overlay back (`pipeline/fx.py`)

### 🎬 Cinematic subtitles + Font ရွေးချယ်ခွင့်
- ပုံစံ: font **Bold**, အရွယ်ကြီး, စာသား အဖြူ + အမည်း outline ထူထူ + shadow ပါးပါး, အောက်ခြေ အလယ် (safe margin), နောက်ခံ box **မပါ** — အလင်းရောင် များတဲ့/နည်းတဲ့ frame နှစ်မျိုးလုံးမှာ ဖတ်ရလွယ် (verification frame တွေမှာ စစ်ပြီး)
- **Font picker** (အားလုံး Myanmar **Unicode**, Zawgyi မဟုတ်) — `assets/fonts/` ထဲ bundle ထည့်ထားပြီး:
  | Key | Font | မှတ်ချက် |
  |---|---|---|
  | `noto` (default) | Noto Sans Myanmar Bold | Google, OFL |
  | `pyidaungsu` | Pyidaungsu 2.3 (Regular+Bold) | Ubuntu `fonts-myanmar` package မှ |
  | `padauk` | Padauk 5.001 (Regular+Bold) | SIL, Debian `fonts-sil-padauk` package မှ |
- Font တိုင်းကို Myanmar block (U+1000–109F) glyph အပြည့်ပါတာ fontTools နဲ့ စစ်ပြီး, တကယ့် subtitle frame ထုတ်ပြီး shaping မှန်တာ မျက်စိနဲ့ စစ်ပြီးမှ ထည့်ထားတယ် (frame: `verification/font_*.png`)

### ⛔ မထည့်ထားတာတွေ
- **Outro/အဆုံးသတ် အပို လုံးဝ မထည့်ပါ** — output ကြာချိန်ဟာ retime လုပ်ထားတဲ့ မူရင်း content ကြာချိန်နဲ့ အတိအကျ တူတယ်
- Copyright feature တွေ (rights checkbox, credit အလိုအလျောက်, intro/outro) — user ဆုံးဖြတ်ချက်အရ **မထည့်ထားပါ**

### Demo-update limitations
- Blur အကွက် တစ်ကွက်တည်း, အချိန်အပိုင်းအလိုက် ခွဲမရ (တစ်လျှောက်လုံး ဝါး)
- Logo လည်း တစ်ပုံတည်း, တစ်နေရာတည်း (animation မပါ)
- Preview download ကို URL အလိုက် cache လုပ်ထား (`previews/`), job က အဲဒီဖိုင်ကို ပြန်သုံး (နှစ်ခါ download မလုပ်)
- TTS မှတ်ချက်: ဒီ sandbox မှာ edge-tts websocket ပိတ်နေပြီး gTTS endpoint 429 ဖြစ်နေချိန် **classic endpoint** က ပြောပေးခဲ့တယ် (demo job ရဲ့ segment အားလုံး `google-classic:my`)

---

## Gemini ဘာသာပြန် + Review/Edit အဆင့် (2026-11-07)

### Gemini (ပထမ ရွေးချယ်မှု — key ရှိမှ)
- Key ထည့်နည်း: server ဘက်မှာ ပဲ — project `.env` ဖိုင်ထဲ `GEMINI_API_KEY=...` (git-ignore လုပ်ထားပြီး) ဒါမှမဟုတ် env var. **Chat ထဲ လုံးဝ မပို့ပါနဲ့**, API response/log ထဲလည်း မပြပါ.
- Key ကို Google AI Studio → "Get API key" ကနေ အခမဲ့ ရနိုင် (free tier, card မလို). Model: `gemini-2.0-flash` (`GEMINI_MODEL` နဲ့ ပြောင်းနိုင်).
- အလုပ်လုပ်ပုံ: segment အားလုံးကို **request တစ်ခုတည်း** နဲ့ ပို့တယ် — သဘာဝကျတဲ့ **ပြောစကား** မြန်မာ, နာမည်/ဂဏန်း မပြောင်း, idiom ကို အဓိပ္ပာယ်နဲ့ ပြန်. ပြန်လာတဲ့ JSON ကို id အတိအကျ စစ်ပြီး တစ်ခုခု လွဲရင်/ကျ quota ပြည့်ရင် **အရင် free chain (Google→Bing→မူရင်း)** ကို အလိုအလျောက် ပြန်ကျတယ်. Segment တိုင်းမှာ `provider: gemini` လို့ မှတ်တယ်.
- မှတ်ချက်: ဒီ sandbox က Google API ဆီ မရောက်လို့ live Gemini ကို ဒီမှာ မစမ်းနိုင်သေး — mocked test (`verification/test_gemini_mock.py`) နဲ့ code path အပြည့်အစုံ စစ်ထားတယ်. Key ထည့်ပြီး သာမန် network မှာ ပထမဆုံး job နဲ့ စစ်ပါ.

### Review / Edit အဆင့် (လူက စစ်ပြီး ပြင်နိုင်)
1. Phase 1: download → transcribe → translate ပြီးရင် job က **`waiting_review`** မှာ ရပ်စောင့်တယ်
2. UI မှာ segment စာရင်း ပေါ်တယ် — အချိန်, အင်္ဂလိပ် မူရင်း, **ပြင်လို့ရတဲ့ မြန်မာ textarea** (ဖုန်းမှာ သုံးလွယ်အောင် အကြီးကြီး စီထား)
3. **"✏️ ပြင်ပြီးဆက်မယ်"** = ပြင်ထားတာတွေနဲ့ ဆက် / **"➡️ ဒီအတိုင်းဆက်မယ်"** = မပြင်ဘဲ ဆက်
4. Phase 2: **နောက်ဆုံး ပြင်ပြီးသား စာသား** ကနေမှ TTS အသံထွက် → retime → Logo/Blur → စာတန်းထိုး → output
- API: `GET /api/jobs/<id>/segments`, `POST /api/jobs/<id>/review {"edits":[{"idx":0,"text_my":"..."}]}`

## စာတန်း Layout ထိန်းချုပ်မှု (2026-11-08)

စာတန်းက frame ထဲမှာ ဘယ်လောက် နေရာယူမလဲ ဆိုတာ Editor ရဲ့ **🔤 စာတန်း ပုံစံ** အပိုင်းမှာ ချိန်လို့ရတယ် (Preview ပုံပေါ်မှာ နမူနာစာတန်း live ပေါ်တယ်):

| ထိန်းချုပ်မှု | အတိုင်းအတာ | မူလ (default) |
|---|---|---|
| စာအရွယ်အစား `size_pct` | 60–160% (မူလ အရွယ်ရဲ့ %) | 100 |
| စာတန်းအကျယ် `width_pct` | frame အကျယ်ရဲ့ 50–100% — စာလုံး လှိမ့်ပုံ (line wrap) ပါ ပြောင်းတယ် | 95 |
| အစွန်းက အကွာအဝေး `bottom_pct` | frame အမြင့်ရဲ့ 2–25% | 10 |
| နေရာ `align` | `bottom` / `top` | bottom |

- **နည်းပညာ**: `pipeline/burn.py` က SRT ကို ASS ဖိုင်အဖြစ် ပြောင်းပြီး PlayResX/PlayResY ကို **ဗီဒီယိုရဲ့ တကယ့် resolution** ထားတယ် — FontSize = (26/288 × အမြင့် × size%), MarginL/R = (100 − width)% တစ်ဝက်စီ, MarginV = bottom% × အမြင့်. ဒါကြောင့် 16:9 ရော **vertical 9:16** ရော အတိုင်းအတာ မှန်တယ်. Default တန်ဖိုးတွေက အရင် cinematic ပုံစံ (FontSize 26 / MarginV 30 @384×288 PlayRes) နဲ့ အတိအကျ ကိုက်အောင် တွက်ထားတာ — `subs` မပါဘဲ job တင်ရင် အရင်အတိုင်း ထွက်တယ်.
- **API**: `POST /api/jobs` မှာ `"subs": {"size_pct":100, "width_pct":92, "bottom_pct":6, "align":"bottom"}` ထည့်နိုင် — server ဘက်မှာ အတိုင်းအတာထဲ clamp လုပ်ပြီး. `subs` mode ရော `both` mode ရော အကျုံးဝင်တယ်.
- စစ်ဆေးထားပုံ: `verification/sublayout_default.png` / `sublayout_narrow.png` / `sublayout_wide.png` (verification.log မှာ အသေးစိတ်).

## စာသား သီးသန့် ထုတ်/ထည့် (2026-11-08)

စာသားကိုပဲ သီးသန့် လိုချင်တာ / ကိုယ့်စာသား ကိုယ်ထည့်ချင်တာ အတွက် — အားလုံးက review API သုံးတဲ့ **segments_my.json store တစ်ခုတည်း** ကိုပဲ ဖတ်/ရေးတာမို့ ပြင်ထားတဲ့ စာသားနဲ့ အမြဲ ထပ်တူကျတယ်. Phase 2 (`both` mode) က အဲဒီ store ကနေပဲ အသံထွက် (TTS) ရော စာတန်းထိုး (burn) ရော လုပ်တာမို့ **ပြောတဲ့စာသားနဲ့ မြင်ရတဲ့ စာတန်းက အမြဲ အတူတူပဲ**.

### 📤 စာသား ထုတ်ယူ (Export) — ဗီဒီယို မလိုဘဲ
- `GET /api/jobs/<id>/export/txt` — မြန်မာ (ပြင်ပြီးသား `text_my`), အပိုင်း တစ်ပိုင်းကို စာပိုဒ် တစ်ပိုဒ်. ဖိုင်: `<id>_my.txt`
- `GET /api/jobs/<id>/export/srt` — အချိန်ပါတဲ့ SRT (နံပါတ် အစဉ်လိုက်, စာမရှိတဲ့ အပိုင်း ကျော်ပြီး နံပါတ် ပြန်စီ)
- `?lang=src` ထည့်ရင် မူရင်း ဘာသာ (`text`) — txt/srt နှစ်မျိုးလုံး ရတယ်
- Segments ရပြီးသား အဆင့် (waiting_review / done) ဆို ချက်ချင်း ဒေါင်းနိုင် — **phase 2 လုပ်စရာ မလို**. UI: review screen နဲ့ job card မှာ ခလုတ် ၄ ခု (📄 မြန်မာ TXT / မြန်မာ SRT / မူရင်း TXT / မူရင်း SRT)
- မသိတဲ့ job → 404, segments မရသေး → 404, `lang` မှား → 400

### 📥 စာသား အပြည့်အစုံ ထည့် (Bulk input)
- Review screen က **"📝 မြန်မာ စာသား အပြည့်အစုံ ထည့်ရန်"** textarea မှာ ကိုယ်တိုင်/အပြင်မှာ ပြန်ထားတဲ့ စာသား အပြည့်ကို ကူးထည့်ပြီး **"အပိုင်းတွေထဲ ခွဲထည့်မယ်"** နှိပ်ရုံပဲ. API: `POST /api/jobs/<id>/review/bulk {"text": "..."}` (waiting_review မှာပဲ ရတယ်, phase 2 မစပါ)
- **ခွဲပုံ စည်းကမ်း**: (၁) စာကြောင်း (line) အလိုက် ခွဲတယ် — ကြောင်း အရေအတွက်က အပိုင်း အရေအတွက်နဲ့ မတူရင် (၂) ဝါကျ အဆုံး **။** နဲ့ **.** အလိုက် ပြန်ခွဲတယ်. အဲဒီနောက် အစဉ်လိုက် ထည့်တယ် — **ပိုနေရင် နောက်ဆုံး အပိုင်းထဲ ပေါင်းထည့်**, **လိုနေရင် ကျန် အပိုင်းတွေ မပြောင်းဘဲ ဒီအတိုင်း ထား**. သိမ်းတဲ့ အပိုင်းတွေ `edited=True` မှတ်တယ်
- ခွဲထည့်ပြီးရင် per-segment box တွေထဲ တန်း ဖြည့်ပေးတယ် — ပြန်စစ်/ပြင်ပြီး ပုံမှန် **"✏️ ပြင်ပြီးဆက်မယ်"** နဲ့ ဆက်လုပ်ရုံပဲ
