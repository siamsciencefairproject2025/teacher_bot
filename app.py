primary_answer = "none_for_now"
secondary_answer = "none_for_now"

from flask import Flask, request, Response, send_file, render_template, jsonify
import os
import uuid
import time
import glob
import logging
from gtts import gTTS
from dotenv import load_dotenv
from googletrans import Translator
from flask_caching import Cache
import bleach
import backoff
import requests  # ← NEW for OpenRouter

load_dotenv()

app = Flask(__name__)

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Caching setup
app.config['CACHE_TYPE'] = 'simple'
cache = Cache(app)

# Create audio folder if not exist
os.makedirs("static/audio", exist_ok=True)

# Translator
translator = Translator()

# Predefined response translations (unchanged)
RESPONSE_TRANSLATIONS = {
    "লাইটটি চালু হয়েছে": "The light has been turned on",
    "লাইটটি বন্ধ হয়েছে": "The light has been turned off",
    "বীজ বপন ব্যবস্থা চালু হয়েছে": "The seed sowing system has been turned on",
    "বীজ বপন ব্যবস্থা বন্ধ হয়েছে": "The seed sowing system has been turned off",
    "কীটনাশক ব্যবস্থা চালু হয়েছে": "The fertilizer system has been turned on",
    "কীটনাশক ব্যবস্থা বন্ধ হয়েছে": "The fertilizer system has been turned off",
    "ওয়াটার পাম্প চালু হয়েছে": "The water pump has been turned on",
    "ওয়াটার পাম্প বন্ধ হয়েছে": "The water pump has been turned off",
    "পরিমাপ করা হচ্ছে... LCD প্যানেল দেখুন": "Measuring... Look at the LCD panel",
    "বন্ধ করা হচ্ছে...": 'Stopping....',
    "রোভার শুরু হচ্ছে।": "Starting rover.",
    "রোভার বন্ধ হচ্ছে।": "Stopping rover."
}

# Load user instructions
SYSTEM_INSTRUCTION_BN = ""
SYSTEM_INSTRUCTION_EN = ""
with open("USER_INSTRUCTIONS_BN.txt", "r") as file:
    SYSTEM_INSTRUCTION_BN = file.read()
with open("USER_INSTRUCTIONS_EN.txt", "r") as file:
    SYSTEM_INSTRUCTION_EN = file.read()

def get_system_instruction(lang):
    return SYSTEM_INSTRUCTION_BN if lang == 'bn' else SYSTEM_INSTRUCTION_EN

def split_text(text, max_length=150):
    sentences = text.split('।' if '.' not in text else '.')
    chunks = []
    current_chunk = ""
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        if len(current_chunk) + len(sentence) <= max_length:
            current_chunk += sentence + ("। " if '.' not in text else ". ")
        else:
            if current_chunk:
                chunks.append(current_chunk.strip())
            current_chunk = sentence + ("। " if '.' not in text else ". ")
    if current_chunk:
        chunks.append(current_chunk.strip())
    return chunks

@backoff.on_exception(backoff.expo, Exception, max_tries=3)
def generate_tts_chunk(text, lang):
    tts = gTTS(text=text, lang=lang, slow=False)
    chunk_mp3 = os.path.join("static", "audio", f"{uuid.uuid4()}.mp3")
    tts.save(chunk_mp3)
    time.sleep(1)
    base = request.host_url.rstrip('/')
    public_path = f"{base}/{chunk_mp3.replace(os.sep, '/')}"
    logger.info("TTS saved: %s -> %s", chunk_mp3, public_path)
    return public_path

def generate_audio_sync(text_chunks, lang):
    audio_urls = []
    for chunk in text_chunks:
        try:
            public_path = generate_tts_chunk(chunk, lang)
            audio_urls.append(public_path)
        except Exception as e:
            logger.error("TTS error saving chunk after retries: %s", e, exc_info=True)
    return audio_urls

def get_english_translation(bn_text):
    if bn_text in RESPONSE_TRANSLATIONS:
        return RESPONSE_TRANSLATIONS[bn_text]
    try:
        return translator.translate(bn_text, src='bn', dest='en').text
    except Exception as e:
        logger.error(f"Translation error: {e}")
        return bn_text + " (Translation unavailable)"

def get_bangla_translation(en_text):
    for bn, en in RESPONSE_TRANSLATIONS.items():
        if en == en_text:
            return bn
    try:
        return translator.translate(en_text, src='en', dest='bn').text
    except Exception as e:
        logger.error(f"Translation error: {e}")
        return en_text + " (অনুবাদ অনুপলব্ধ)"

@app.route('/')
def serve_webpage():
    return render_template('homepage.html')

@app.route('/chat')
def chat():
    return render_template('chat.html')

@app.route('/moveauto')
def moveauto():
    return render_template('movement_auto.html')

@app.route('/movemanual')
def movemanual():
    return render_template('movemen_manual.html')

@cache.cached(timeout=300, query_string=True)
@app.route('/ask', methods=['GET'])
def ask_bot():
    global primary_answer, secondary_answer
    primary_answer = "none_for_now"
    question = bleach.clean(request.args.get('q', ''))
    lang = request.args.get('lang', 'bn')
    if not question:
        return jsonify({'error': 'Missing question'}), 400

    try:
        # === OPENROUTER INTEGRATION (replaces Gemini) ===
        system_instruction = get_system_instruction(lang)
        user_prompt = f"প্রশ্ন: {question}\n\nউত্তর দিন:" if lang == 'bn' else f"Question: {question}\n\nAnswer:"

        api_key = os.getenv("OPENROUTER_API_KEY")
        if not api_key:
            return jsonify({'error': 'OpenRouter API key is missing in .env'}), 500

        response = requests.post(
            url="https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": "openai/gpt-oss-120b:free",   # ← your model from the example
                "messages": [
                    {"role": "system", "content": system_instruction},
                    {"role": "user", "content": user_prompt}
                ]
            }
        )
        response.raise_for_status()
        data = response.json()
        primary_answer = data['choices'][0]['message']['content'].strip()

        # Get secondary translation (unchanged)
        secondary_answer = get_english_translation(primary_answer) if lang == 'bn' else get_bangla_translation(primary_answer)

        # Generate audio (unchanged)
        primary_chunks = split_text(primary_answer)
        secondary_chunks = split_text(secondary_answer)

        audio_urls_primary = generate_audio_sync(primary_chunks, lang)
        audio_urls_secondary = generate_audio_sync(secondary_chunks, 'en' if lang == 'bn' else 'bn')

        logger.info("Audio URLs primary: %s", audio_urls_primary)
        logger.info("Audio URLs secondary: %s", audio_urls_secondary)

        cleanup_audio_files()

        return jsonify({
            'answer_bn': primary_answer if lang == 'bn' else secondary_answer,
            'answer_en': secondary_answer if lang == 'bn' else primary_answer,
            'audio_urls_bn': audio_urls_primary if lang == 'bn' else audio_urls_secondary,
            'audio_urls_en': audio_urls_secondary if lang == 'bn' else audio_urls_primary
        })

    except Exception as e:
        logger.error(f"Error: {str(e)}", exc_info=True)
        return jsonify({'error': str(e)}), 500

# (All other functions — cleanup_audio_files, get_audio, esp32-receive, esp32-receive-movement, esp32-movement — remain EXACTLY the same as your original code)
def cleanup_audio_files():
    max_age = 3600
    for file in glob.glob("static/audio/*.mp3"):
        if os.path.getmtime(file) < time.time() - max_age:
            try:
                os.remove(file)
            except Exception as e:
                logger.error("Error removing old audio file %s: %s", file, e)

@app.route('/static/audio/<filename>')
def get_audio(filename):
    return send_file(f'static/audio/{filename}', mimetype='audio/mpeg')

# ... (rest of your ESP32 routes unchanged - I kept them identical)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
