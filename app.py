primary_answer = "none_for_now"
secondary_answer = "none_for_now"

from flask import Flask, request, Response, send_file, render_template, jsonify
import os
import uuid
import time
import glob
import logging
import requests
import json
from gtts import gTTS
from dotenv import load_dotenv
from googletrans import Translator
from flask_caching import Cache
import bleach
import backoff

load_dotenv()

app = Flask(__name__)

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# OpenRouter config
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openai/gpt-oss-120b:free")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Caching setup
app.config['CACHE_TYPE'] = 'simple'
cache = Cache(app)

# Create audio folder if not exist
os.makedirs("static/audio", exist_ok=True)

# Translator (using sync version)
translator = Translator()

# Predefined response translations
RESPONSE_TRANSLATIONS = {
    "লাইটটি চালু হয়েছে": "The light has been turned on",
    "লাইটটি বন্ধ হয়েছে": "The light has been turned off",
    "বীজ বপন ব্যবস্থা চালু হয়েছে": "The seed sowing system has been turned on",
    "বীজ বপন ব্যবস্থা বন্ধ হয়েছে": "The seed sowing system has been turned off",
    "কীটনাশক ব্যবস্থা চালু হয়েছে": "The fertilizer system has been turned on",
    "কীটনাশক ব্যবস্থা বন্ধ হয়েছে": "The fertilizer system has been turned off",
    "ওয়াটার পাম্প চালু হয়েছে": "The water pump has been turned on",
    "ওয়াটার পাম্প বন্ধ হয়েছে": "The water pump has been turned off",
    "পরিমাপ করা হচ্ছে... LCD প্যানেল দেখুন": "Measuring... Look at the LCD panel",
    "বন্ধ করা হচ্ছে...":'Stopping....',
    "রোভার শুরু হচ্ছে।":"Starting rover.",
    "রোভার বন্ধ হচ্ছে।":"Stopping rover."
}

SYSTEM_INSTRUCTION_BN = ""
SYSTEM_INSTRUCTION_EN = ""

with open("USER_INSTRUCTIONS_BN.txt","r") as file:
    SYSTEM_INSTRUCTION_BN = file.read()
with open("USER_INSTRUCTIONS_EN.txt","r") as file:
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

def call_openrouter(system_instruction, user_message):
    """Call OpenRouter API and return the response text."""
    resp = requests.post(
        url=OPENROUTER_URL,
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
        },
        data=json.dumps({
            "model": OPENROUTER_MODEL,
            "messages": [
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": user_message}
            ]
        }),
        timeout=30
    )
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"].strip()

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
    global primary_answer
    global secondary_answer
    primary_answer = "none_for_now"
    question = bleach.clean(request.args.get('q', ''))
    lang = request.args.get('lang', 'bn')
    if not question:
        return jsonify({'error': 'Missing question'}), 400

    try:
        system_instr = get_system_instruction(lang)
        user_msg = f"প্রশ্ন: {question}\n\nউত্তর দিন:" if lang == 'bn' else f"Question: {question}\n\nAnswer:"

        primary_answer = call_openrouter(system_instr, user_msg)

        # Get secondary translation
        secondary_answer = get_english_translation(primary_answer) if lang == 'bn' else get_bangla_translation(primary_answer)

        # Generate audio synchronously
        primary_chunks = split_text(primary_answer)
        secondary_chunks = split_text(secondary_answer)

        audio_urls_primary = generate_audio_sync(primary_chunks, lang)
        audio_urls_secondary = generate_audio_sync(secondary_chunks, 'en' if lang == 'bn' else 'bn')

        logger.info("Audio URLs primary: %s", audio_urls_primary)
        logger.info("Audio URLs secondary: %s", audio_urls_secondary)

        if not audio_urls_primary:
            logger.warning("Primary audio failed; falling back to text-only")

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

@app.route("/esp32-receive/", methods=["GET"])
def esp32_receive():
    print(request.content_type)
    command_triggers = {
        "light_on": ["লাইটটি চালু হয়েছে", "The light has been turned on", "Light has been turned ON"],
        "light_off": ["লাইটটি বন্ধ হয়েছে", "The light has been turned off", "Light has been turned OFF"],
        "seed_sow_on": ["বীজ বপন ব্যবস্থা চালু হয়েছে", "The seed sowing system has been turned on", "Seed sowing system has been turned ON"],
        "seed_sow_off": ["বীজ বপন ব্যবস্থা বন্ধ হয়েছে", "The seed sowing system has been turned off", "Seed sowing system has been turned OFF"],
        "fertilizer_on": ["কীটনাশক ব্যবস্থা চালু হয়েছে", "The fertilizer system has been turned on", "Fertilizer system has been turned ON"],
        "fertilizer_off": ["কীটনাশক ব্যবস্থা বন্ধ হয়েছে", "The fertilizer system has been turned off", "Fertilizer system has been turned OFF"],
        "water_pump_on": ["ওয়াটার পাম্প চালু হয়েছে", "The water pump has been turned on", "Water pump has been turned ON"],
        "water_pump_off": ["ওয়াটার পাম্প বন্ধ হয়েছে", "The water pump has been turned off", "Water pump has been turned OFF"],
        "start_measuring_soil_moisture": ["পরিমাপ করা হচ্ছে... LCD প্যানেল দেখুন", "Measuring... Look at the LCD panel", "MEASURING.... LOOK AT THE LCD PANEL"],
        "stop_measuring_soil_moisture": ["বন্ধ করা হচ্ছে...","Stopping....","STOPPING...."],
        "start_rover": ["রোভার শুরু হচ্ছে।","Starting rover.","STARTING ROVER."],
        "stop_rover": ["রোভার বন্ধ হচ্ছে।","Stopping rover.","STOPPING ROVER."]
    }

    answers = [primary_answer.lower(), secondary_answer.lower()]
    for cmd, phrases in command_triggers.items():
        for phrase in [p.lower() for p in phrases]:
            for ans in answers:
                if phrase in ans:
                    return cmd

    return "none_for_now"

@app.route("/esp32-receive-movement", methods=["POST"])
def esp32_receive_movement():
    global clever_way
    data = request.get_json()
    clever_way = data
    content_type = request.content_type
    print(content_type)
    print(data)

    height = data.get('height')
    width = data.get('width')
    num_rows = data.get('num_rows')
    orientation = data.get('orientation')
    distance = data.get('distance')

    print(f"Received data: Height = {height} ft, Width = {width} ft, Rows = {num_rows}, Orientation = {orientation}, Distance = {distance} ft")

    total_area = height * width
    print(f"Calculated total land area: {total_area} sq ft")

    movement_plan = {
        "rows": num_rows,
        "distance_between_rows": distance,
        "orientation": orientation,
        "field_dimensions": {"height": height, "width": width}
    }

    return jsonify({
        "message": "Data received successfully!",
        "received_data": data,
        "calculated_area": total_area,
        "movement_plan": movement_plan
    }), 200

@app.route("/esp32-movement/", methods=["GET"])
def esp32_movement():
    row = clever_way["num_rows"]
    orient = clever_way["orientation"]
    width = clever_way["width"]
    height = clever_way["height"]
    distance = clever_way["distance"]
    instruction_str = ""
    if orient == "vertical":
        for i in range(1, row):
            if i % 2 == 0:
                instruction_str += "FLfL"
            else:
                instruction_str += "FRfR"
        instruction_str += "F"
        final_str = str(height)+"-"+str(distance)+"_"+instruction_str
        return final_str
    else:
        for i in range(1, row):
            if i % 2 == 0:
                instruction_str += "FRfR"
            else:
                instruction_str += "FLfL"
        instruction_str = "R"+instruction_str+"F"
        final_str = str(width)+"-"+str(distance)+"_"+instruction_str
        return final_str


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
