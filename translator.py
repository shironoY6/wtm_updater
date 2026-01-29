import os
from time import sleep
from random import randint

import yaml
from google.cloud import translate_v2

#### Google translate functions

translate_v2_client = translate_v2.Client()


def get_translation_google_translate_v2(text: str, target: str = "ja") -> str:
    """Translates text into the target language.

    Target must be an ISO 639-1 language code.
    See https://g.co/cloud/translate/v2/translate-reference#supported_languages
    """

    if isinstance(text, bytes):
        text = text.decode("utf-8")

    # Text can also be a sequence of strings, in which case this method
    # will return a sequence of results for each text.
    result = translate_v2_client.translate(text, target_language=target)

    print("Text: {}".format(result["input"]))
    print("Translation: {}".format(result["translatedText"]))
    print("Detected source language: {}".format(result["detectedSourceLanguage"]))

    return result["translatedText"]


#### Telegram translate functions

def get_translation_telegram(tg, msg=None, text=None, return_formattedText=False):
    if msg:
        data = {
            "@type": "translateMessageText",
            "chat_id": msg["chat_id"],
            "message_id": msg["id"],
            "to_language_code": "ja",
        }
    elif text:
        if isinstance(text, str):
            formattedText = {"@type": "formattedText", "text": text, "entities": []}
        elif (
            isinstance(text, dict)
            and "@type" in text
            and text["@type"] == "formattedText"
        ):
            formattedText = text
        else:
            print("input should be str or formattedText.")
            return ""

        data = {
            "@type": "translateText",
            "text": formattedText,
            "to_language_code": "ja",
        }
    else:
        print("provide either msg or text")
        return ""

    r = tg._send_data(data)
    r.wait()
    if not r.error:
        if return_formattedText:
            return r.update
        else:
            return r.update["text"]

    return ""


#### obsolete DeepL functions

DEEPL_KEY = os.getenv("DEEPL_KEY")
if DEEPL_KEY:
    import deepl
    translator = deepl.Translator(DEEPL_KEY)

# swapping common machine translation errors
with open("correction_data.yaml", "r") as f:
    correction_dict = yaml.load(f, Loader=yaml.Loader)


def correctDeepL(text):
    for k, v in correction_dict.items():
        text = text.replace(k, v)
    return text


def deeplJP(text, retry=3):
    """text: str or list of str"""
    flag = True
    for n in range(retry):
        while flag:
            # to avoid processes rushing to DeepL APIs
            print(f"deepl API call trial {n+1}")
            sleep(randint(1, 6) * n)
            try:
                textjp = translator.translate_text(text, target_lang="JA")
                flag = False
                break
            except:
                print("deepl翻訳 API error.")
                continue
            break
    if textjp.text:
        return correctDeepL(textjp.text)
    else:
        None


def show_usage():
    r = translator.get_usage()
    c = r.character.count
    l = r.character.limit
    print(f"{c} / {l}. {c/l*100}% used.")

