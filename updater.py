import json
import os
import re
import base64
import platform
import sys

from urllib.parse import urlparse
from random import randint
from datetime import tzinfo, datetime, date, timedelta
from time import sleep, time

import requests
from pytz import timezone
from telegram.client import Telegram
from bs4 import BeautifulSoup

import db_utils as dbutils
from check_redirect import get_final_url_with_selenium
from translator import get_translation_google_translate_v2, get_translation_telegram

# OCR_SPACE = os.getenv("OCR_SPACE") # obsolete, switched to Telegram Grok
chat_id_grok = os.getenv("chat_id_grok")  # For OCR and summarization. This requires Telegram Premium.

# source channel
WTM = -1001161789591  # We The Media
playground_IN = os.getenv("playground_IN")  # test input channel ID for dev (To mimick a post forwarded to WTM by contributors)

# post target channel
WTMjp = -1001501671025  # WeTheMedia_jp日本語訳チャネル🇯🇵
playground_OUT = os.getenv("playground_OUT")  # test output channel ID for dev

DEPLOYMENT_STAGE = os.getenv("DEPLOYMENT_STAGE", "test")  # "prod" or "test"

channel2post_mapping = {WTM: WTMjp, playground_IN: playground_OUT}
# channel2post_mapping = {WTM: WTMjp, WTM: playground_OUT}

# error report Telegram chat
sendErrorTo = os.getenv("sendErrorTo")


# target locale (should have been Tokyo but it's too late)
amsterdam = timezone("Europe/Amsterdam")
tokyo = timezone("Asia/Tokyo")
EST = timezone("US/Eastern")

CHANNEL_NOT_FOUND = "転送元チャネル名の取得エラー。"
NO_SIGNATURE = "管理者無記名投稿"

media_message_types = [
    "messagePoll",
    "messageDocument",
    "messageAnimation",
    "messagePhoto",
    "messageVideo",
]

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
}


def get_channel_info(tg, chat_id):
    r = tg.get_chat(chat_id)
    r.wait()
    if not r.error and r.update["type"]["is_channel"]:
        return r.update["title"]
    else:
        # channel might have been upgraded to supergroup https://stackoverflow.com/a/74714481
        return CHANNEL_NOT_FOUND


def show_chat_list(tg):
    # get_chats will list all channels, groups, 1:1 chats I initiated and have not left.
    result = tg.get_chats(9223372036854775807)
    result.wait()

    chats = result.update["chat_ids"]

    # get each chat
    print("Chat List")
    chat_map = {}
    for chat_id in chats:
        r = tg.get_chat(chat_id)
        r.wait()
        title = r.update["title"]
        print("  %20d\t%s" % (chat_id, title))
        chat_map[chat_id] = r.update

    return chat_map


def get_messageFrom_tme(tg, link=None):
    if link is None:
        link = input("Link=")

    r = tg._send_data(
        {
            "@type": "getInternalLinkType",
            "link": link,
        }
    )
    r.wait()
    if r.error:
        print(f"Internal link for {link} was not found: {r.error_info}")
        return
    else:
        url = r.update["url"]
        r = tg._send_data(
            {
                "@type": "getMessageLinkInfo",
                "url": url,
            }
        )
        r.wait()
        if r.error:
            print(f"MessageLinkInfo for {url} was not found: {r.error_info}")
            return
        else:
            return r.update["message"]


def retreive_messages(tg, chat_id, receive_limit=100, from_message_id=0):
    "taken from https://github.com/alexander-akhmetov/python-telegram/blob/master/examples/chat_stats.py"
    receive = True
    stats_data = []

    while receive:
        response = tg.get_chat_history(
            chat_id=chat_id,
            limit=1000,
            from_message_id=from_message_id,
        )
        response.wait()
        if not response.error and response.update is not None:
            # dict_keys(['@type', 'total_count', 'messages', '@extra'])
            stats_data += response.update["messages"]
            # from_message_id = response.update['messages'][-1]['id']
            if response.update.get("messages"):
                from_message_id = min([m["id"] for m in response.update["messages"]])
                total_messages = len(stats_data)
                if total_messages > receive_limit or not response.update["total_count"]:
                    receive = False
            else:  # if update is an empty list, we finish
                break

            print(f"[{total_messages}/{receive_limit}] received")
        else:
            print("response.error_info", response.error_info)
            break

    return stats_data


def downloadFile(tg, file_id):
    r = tg._send_data(
        {
            "@type": "downloadFile",
            "file_id": file_id,
            "limit": 0,
            "offset": 0,
            "priority": 1,
            "synchronous": True,
        }
    )
    r.wait()
    if not r.error:
        return r.update


def getMessageLink(tg, chat_id, message_id, album_flag=False):
    """
    MessageLink is the t.me/s/{chanel_name}/{MessageLink_id} address that
    users can see without Telegram client.
    """
    data = {
        "@type": "getMessageLink",
        "chat_id": chat_id,
        "message_id": message_id,
        "for_album": album_flag,
        "for_comment": not album_flag,
    }

    return tg._send_data(data)


def guess_authors(tg, msg):
    author_signature = msg["author_signature"] or NO_SIGNATURE

    origin_channel_name, origin_author_signature = "", ""
    forward_info = msg.get("forward_info")
    if forward_info:
        if forward_info["origin"]["@type"] in [
            "messageForwardOriginChannel",
            "messageOriginChannel",
        ]:
            origin_channel_name = get_channel_info(
                tg, chat_id=forward_info["origin"]["chat_id"]
            )
            origin_author_signature = forward_info["origin"]["author_signature"]
        elif forward_info["origin"]["@type"] == "messageForwardOriginHiddenUser":
            origin_author_signature = msg["forward_info"]["origin"].get("sender_name")

    return author_signature, origin_channel_name, origin_author_signature


def create_author_section(
    author_signature, origin_channel_name, origin_author_signature
):
    authors = f"WTM投稿者：{author_signature}"
    if origin_channel_name == CHANNEL_NOT_FOUND:
        authors += "\n" + CHANNEL_NOT_FOUND
        authors += (
            f"転送元の({origin_author_signature}の投稿）"
            if origin_author_signature and author_signature != origin_author_signature
            else ""
        )
    else:
        authors += (
            f"\n転送元チャネル：{origin_channel_name}" if origin_channel_name else ""
        )
        authors += (
            f"　({origin_author_signature}の投稿）"
            if origin_author_signature and author_signature != origin_author_signature
            else ""
        )
    return authors + "\n\n"


def getWebPagePreview(tg, url):
    "Note that this is not getWebPageInstantView."
    data = {
        "@type": "getWebPagePreview",
        "url": url,
    }
    return tg._send_data(data)


def parse_preview(webPageInstantView):
    def _finditem(obj, key):
        "recursively go through a nested dict and find all values by the key"
        if key in obj and isinstance(obj[key], str):
            return obj[key]
        for k, v in obj.items():
            if isinstance(v, dict):
                return _finditem(v, key)
        return None

    pb = webPageInstantView["page_blocks"]
    # cover_pic = [block['cover'] for block in pb if 'cover' in block.keys()]
    title = [block["title"] for block in pb if "title" in block.keys()]
    if title:
        title = title[0].get("text", "")
    else:
        title = ""

    author = [block["author"] for block in pb if "author" in block.keys()]
    if author:
        author = _finditem(author[0], "text")  # author[0].get('text', '')
    else:
        author = ""

    publish_date = [
        block["publish_date"] for block in pb if "publish_date" in block.keys()
    ]
    if publish_date:
        publish_date = publish_date[0]
    else:
        publish_date = ""

    blcs = [block["text"] for block in pb if "text" in block.keys()]
    description = ""
    desc = []
    for blc in blcs:
        extracted = _finditem(blc, "text")
        if extracted:
            desc.append(extracted)
        else:
            extracted = _finditem(blc, "texts")
            if extracted:
                desc.append(extracted)
    description = " ".join(desc)
    description = description.replace("\xa0", " ")
    if len(description) > 500:
        description = description[:500]

    return title, author, publish_date, description


def find_text(msg, verbose=False):
    """
    Find and return the formattedText object to translate from various message types:
    ['messagePhoto', 'messageVideo', 'messageText', 'messageAnimation', 'messagePoll', 'messageDocument', 'messagePinMessage']

    - `text` at the content level:
        message, formattedText, messageText,
    - `caption` in the content (can be empty):
        messageDocument, messageAnimation, messagePhoto, messageVideo
    - no text:
        messagePinMessage
    """

    content_type = msg["content"]["@type"]
    if content_type in [
        "messagePhoto",
        "messageVideo",
        "messageAnimation",
        "messageDocument",
    ]:
        return msg["content"]["caption"]
    if content_type in ["messageText"]:
        return msg["content"]["text"]
    if content_type in ["messagePoll"]:
        return msg["content"]["poll"]["question"]

    print(content_type, " not supported.")
    return {"@type": "formattedText", "text": "", "entities": []}


def get_file_id(msg):
    try:
        if "video" in msg["content"]:
            return msg["content"]["video"]["video"]["id"]
        elif "web_page" in msg["content"]:
            return msg["content"]["web_page"]["video"]["video"]["id"]
        elif "photo" in msg["content"]:
            # must be smaller than 1Mbytes for ocr space free quota
            smallerThan1M = [
                m
                for m in msg["content"]["photo"]["sizes"]
                if m["photo"]["size"] < 1024**2
            ]
            if smallerThan1M:
                smallerThan1M = sorted(
                    smallerThan1M, key=lambda m: m["photo"]["size"], reverse=True
                )
                return smallerThan1M[0]["photo"]["id"]
    except KeyError:
        print("file_id not found or too old.", msg["content"])
        return


def get_base64Image(tg, msg, use_thumnail=False):
    if use_thumnail:
        return msg["content"]["photo"]["minithumbnail"]["data"], ""

    file_id = get_file_id(msg)
    if not file_id:
        print("No file_id")
        return None, ""
    r = downloadFile(tg, file_id)
    fp = r["local"]["path"]
    fname, extension = os.path.splitext(fp)

    base64image = ""
    with open(fp, "rb") as f:
        buf = base64.b64encode(f.read()).decode()
        if extension.lower() in [".jpeg", ".jpg"]:
            base64image = f"data:image/jpeg;base64,{buf}"
        elif extension.lower() in [".tiff", ".tif"]:
            base64image = f"data:image/tiff;base64,{buf}"
        elif extension.lower() in [".png", ".bmp", ".gif"]:
            base64image = f"data:image/{extension.lower()[1:]};base64,{buf}"

    if not base64image:
        print("file type not compatible", extension)
        return None, ""
    else:
        return base64image, extension


def get_grok_response(tg, question_date, timeout=64):
    start_time = time()
    grok_response = None
    while time() - start_time < timeout:
        r = tg.get_chat(chat_id_grok)
        r.wait()
        if int(r.update["last_message"]["date"]) > int(question_date):
            content = r.update["last_message"]["content"]
            text = content["text"]["text"] if "text" in content else ""

            # XML-like tag pattern
            tag_pattern = r"<([a-zA-Z][a-zA-Z0-9_-]*)\b[^>]*>(.*?)</\1>"

            # タグを検索
            match = re.search(tag_pattern, text, re.DOTALL)
            if match:
                text = match.group(2).strip()
            else:
                text = text.strip()

            if text.startswith("{"):
                try:
                    text = re.sub(r'\\([^\\ntbfru"])', r"\1", text)
                    grok_response = json.loads(text)
                except json.JSONDecodeError as e:
                    print(f"JSON parsing error: {e}\n{text}")
                    return None
                break
            else:
                grok_response = text

        sleep(4)
    return grok_response


def get_details_from_GrokOCR(tg, msg):
    # remoteFile can't be used here as GrokAI has no access to WTM channel.
    file_id = get_file_id(msg)
    u = downloadFile(tg, file_id)
    fp = u["local"]["path"]

    direction = "この画像にあるテキストを抽出し、原文とその日本語解説を最長で600字までの簡潔な説明文をjsonとして出力してください。jsonのkeyは extractedText と explanation としてください。JSONオブジェクトを直接返してください。テキストとしてのエスケープや余計なバックスラッシュを含めないでください。"
    data = {
        "@type": "sendMessage",
        "chat_id": chat_id_grok,
        "input_message_content": {
            "@type": "inputMessagePhoto",
            "photo": {"@type": "inputFileLocal", "path": fp},
            "caption": {"@type": "formattedText", "text": direction, "entities": []},
        },
    }
    r = tg.send_message(chat_id_grok, "/newchat")
    r.wait()
    sleep(2)
    r = tg._send_data(data)
    r.wait()
    question_date = r.update["date"]

    return get_grok_response(tg, question_date)


def get_grok_summary(tg, url):
    txt = f"""{url}

上のURLの内容について、ついて下のような５つの項目を抽出して

author: 著者名（例：ジャーナリスト名、ブログ主名をそのまま訳さずに）
media: メディアの名前（例：X、NYT、Substack）
onelinesummary: 日本語で一行サマリー
detailedsummary: 日本語で詳細サマリー。内容を捉えつつも上限300字で短めが好ましい。
bulletpoint: 日本人に馴染みのない用語について箇条書きの用語説明。bullet pointには \u2022 を使用。

以下の形式で[]で囲まれた部分を対応する抽出データで置き換え、人間が読めるようにレンダリングしたテキストをデータとして出力して下さい。

[onelinesummary]
[author]による[media]での記事・投稿
[url]
[detailedsummary]

[bulletpoint]
"""
    sleep(0.5)
    print("Getting the Grok summary for", url)
    r = tg.send_message(chat_id_grok, "/newchat")
    r.wait()

    sleep(1.5)
    r = tg.send_message(chat_id_grok, txt)
    r.wait()
    question_date = r.update["date"]
    sleep(8)

    grok_response = get_grok_response(tg, question_date)
    return grok_response


# def get_text_from_OCR(tg, msg):
#     base64image, extension = get_base64Image(tg, msg)
#     print(extension)
#     if extension and len(extension) > 0:
#         file_type = extension[1:].lower()
#     elif base64image and len(base64image) > 1024:
#         base64image, extension = get_base64Image(tg, msg, use_thumnail=True)
#     else:
#         return f"Image {extension} not supported."

#     # OCRSpaceUrl = f"https://api.ocr.space/parse/imageurl?apikey={OCR_SPACE}&url={imageURL}"
#     if file_type:
#         data = {"base64image": base64image, "filetype": file_type}
#     else:
#         data = {"base64image": base64image}

#     try:
#         r = requests.post(
#             "https://api.ocr.space/parse/image",
#             headers={"apikey": OCR_SPACE},
#             data=data,
#         )
#     except requests.exceptions.ConnectTimeout:
#         print("ocr.space ConnectTimeout")
#         return ""

#     if r.ok:
#         rj = r.json()
#         OCRExitCode = rj["OCRExitCode"]
#         if OCRExitCode == 1:
#             return rj["ParsedResults"][0]["ParsedText"]
#         print(rj)

#     return ""


def get_redirected_url(url, timeout=10):
    try:
        url = get_final_url_with_selenium(url)
    except:
        print("Redirection could not be checked:", url)

    return url


def render_grok_summary_json(gr=""):
    "only for the json from web browser via Copy btn"
    try:
        if isinstance(gr, str):
            gr = json.loads(gr)
        print(gr["FormattedText"]["text"].replace("*", ""))
    except:
        print("corrupt data")


def is_blacklisted(url):
    prefixes = [
        "https://t.me/disclosetv/",  # never works
        "https://t.me/amthinkTV",
        "https://archive.ph/",  # often Grok fails to load
        "https://archive.is/",  # often Grok fails to load
        "https://postmillennialnews.com/",  # short URL to https://thepostmillennial.com/
        "https://t.co/",
        "https://linktr.ee/",
        "https://youtu.be/",
        "https://rumble.com/",
        "https://truthsocial.com/",
        # these are redundant with the posts
        "https://t.me/WeTheMedia",
        "https://x.com/",  # to reduce grok usage
        "https://twitter.com/",
        "https://fxtwitter.com/",  # another x.com redirection url
        "https://vxtwitter.com/",
        "https://fixupx.com/",
        "https://www.x.com/",
        # "https://x.com/dotconnectinga/",
        # "https://x.com/BadlandsMedia_/",
        # "https://x.com/wethemedia17/",
        # "https://x.com/WeTheMedia17/",
    ]
    if any(url.startswith(prefix) for prefix in prefixes):
        return True
    elif not "status" in url and ("twitter.com" in url or "x.com" in url):
        # it has to be a particular post, not the account
        return True
    else:
        return False


def extract_urls_from_entity(entities):
    urls = []
    for entity in entities:
        if "url" in entity["type"]:
            url = entity["type"]["url"]
            if not is_blacklisted(url):
                url = get_redirected_url(url)
                urls.append(url)

    return list(set(urls))


def extract_urls(texten):
    # https://www.i2tutorials.com/match-urls-using-regular-expressions-in-python/
    regex = r"(http|ftp|https):\/\/([\w\-_]+(?:(?:\.[\w\-_]+)+))([\w\-\.,@?^=%&:/~\+#]*[\w\-\@?^=%&/~\+#])?"
    urls = []
    for urlpart in re.findall(regex, texten, 0):
        url = urlpart[0] + "://" + "".join(urlpart[1:])
        print(url)
        if not is_blacklisted(url):
            url = get_redirected_url(url)
            urls.append(url)
    return urls


def extract_target_url_from_source(url, session=None):
    "Extract the source URL from disclosetv, archive.is, archive.ph"
    if not isinstance(session, requests.sessions.Session):
        response = requests.get(url, headers=headers)
    else:
        response = session.get(url)
    if response.ok:
        soup = BeautifulSoup(response.text, "html.parser")

        if "disclose.tv" in url:
            div = soup.find(id="c_sum_info")
            if div:
                return div.next.attrs["href"]
        elif "//archive." in url:
            saved_from_input = soup.find("input", {"name": "q", "type": "text"})
            if saved_from_input and saved_from_input.get("value"):
                saved_from_url = saved_from_input["value"]
                print("Saved from URL:", saved_from_url)
                return saved_from_url

    print("An error from the source website: ", response.status_code)
    return url


def replace_redirection(urls, session=None):
    replaced_urls = set()
    for url in set(urls):
        if "www.disclose.tv" in url:
            new_url = extract_target_url_from_source(url, session)
            replaced_urls.add(new_url)
        elif "//archive." in url and False:  # stop this for now
            new_url = extract_target_url_from_source(url, session)
            replaced_urls.add(new_url)
        sleep(3)
    return list(replaced_urls)


def extract_url_from_msg(msg, session=None):
    entities = []

    # if formatted URL links are embedded
    if "text" in msg["content"]:
        entities = msg["content"]["text"]["entities"]
    elif "caption" in msg["content"]:
        entities = msg["content"]["caption"]["entities"]

    extracted = extract_urls_from_entity(entities)

    # see if link preview is available
    if "web_page" in msg["content"]:
        url = msg["content"]["web_page"]["url"]
        if not is_blacklisted(url):
            extracted.append(url)

    if not extracted:
        extracted = extract_urls(find_text(msg)["text"])

    extracted = replace_redirection(extracted, session)
    return list(set(extracted))


def validate_params(msg_id, author, PostDate):
    assert type(msg_id) == int and msg_id  # e.g. 47018147840
    assert type(author) == str and author  # some text
    assert type(PostDate) == datetime  # datetime object
    return True


def get_timestamp(PostDate, tz=None, strformat="%m/%d %H:%M:%S"):
    return (
        datetime.fromtimestamp(PostDate.timestamp()).astimezone(tz).strftime(strformat)
    )


def forward_msg(tg, channel2post, msg_id, mock=False):
    # https://core.telegram.org/tdlib/docs/classtd_1_1td__api_1_1forward_messages.html
    # message_id: normally an integer. can be a list when it's album.

    data = {
        "@type": "forwardMessages",
        "chat_id": channel2post,  # '@WTM_deepl_jp',
        "from_chat_id": WTM,  # normally '@WeTheMedia' but in dev overwritten to playground
        "message_ids": [msg_id],  # from get_chat_history and 'id' key
        # "send_copy": False,
        # "remove_caption": False,
    }
    if mock:
        print(f"forward_msg:  forwaring msg id {message_ids}. {data}")
        return True
    else:
        r = tg._send_data(data, block=True)
        if r is None:
            return None
        elif r.error:
            return None
        elif r.update["messages"] == [None]:
            return None
        else:
            return r


def get_entities(msg):
    try:
        content_text = msg["content"]["text"]["entities"]
    except KeyError:
        content_text = None

    try:
        content_caption = msg["content"]["caption"]["entities"]
    except KeyError:
        content_caption = None

    print(
        f"get_entities:\ncontent_text = {content_text}\ncontent_caption = {content_caption}"
    )

    if content_text:
        return content_text
    if content_caption:
        return content_caption

    return None


def compute_uft16_offset(prepend_text):
    # https://stackoverflow.com/a/39280419
    # https://github.com/python-telegram-bot/python-telegram-bot/issues/400
    return len(prepend_text.encode("utf-16-le")) // 2


def offset_entiries(entities, offset=0):
    return [
        {
            **entity,
            "offset": entity["offset"] + offset,
        }
        for entity in entities
    ]


def generate_post(
    texten_formatted,
    textjp_formatted,
    author,
    origin_channel_name,
    origin_author_signature,
    PostDate,
    link,
    previewlink,
    grok_responses,
    media_post=False,
):
    author_section = create_author_section(
        author, origin_channel_name, origin_author_signature
    )

    timestampTokyo = (
        datetime.fromtimestamp(PostDate.timestamp())
        .astimezone(tokyo)
        .strftime("%Y年%-m月%-d日 %H:%M:%S")
    )
    timestampCEST = (
        datetime.fromtimestamp(PostDate.timestamp())
        .astimezone(amsterdam)
        .strftime("%-m月%-d日 %H:%M:%S")
    )
    timestampEST = (
        datetime.fromtimestamp(PostDate.timestamp())
        .astimezone(EST)
        .strftime("%-m月%-d日 %H:%M:%S")
    )
    timestamp = f"日本 {timestampTokyo}, NY {timestampEST}, CEST {timestampCEST}"

    footnote = f"\n--------\n投稿日時: {timestamp}" + f"\n{previewlink} 👉"

    textjp = textjp_formatted["text"]
    offsetjp = 0
    texten = texten_formatted["text"]
    offseten = 0

    if any(["extractedText" in gr for gr in grok_responses]):
        short_media_post = True
    else:
        short_media_post = False

    if textjp:
        insert = "(翻訳)\n"
        offsetjp += compute_uft16_offset(insert)
        textjp = insert + textjp
    else:
        insert = "翻訳エラー　もしくは　テキスト未検出。　（解説用プレイスホルダー）"
        offsetjp += compute_uft16_offset(insert)
        textjp = insert + textjp

    if texten:
        if not media_post or short_media_post:
            # eng_len = 4096 - 80  # 4096 is the max allowed in Telegram.
            total_length = len(author_section + texten + textjp + footnote) + 2
            if total_length > 4000:
                eng_len = len(texten) - (total_length - 4096)
                texten = texten[:eng_len] + "\n\n"
            else:
                texten += "\n\n"

    # combine
    entities = []
    text = author_section
    if media_post and not short_media_post:
        offsetjp += compute_uft16_offset(author_section)
        text += textjp
        text += "\n\n(Orinial text starting with: "
        offseten += compute_uft16_offset(text)
        text += f"{texten[:50]}...)"

        entities += offset_entiries(textjp_formatted["entities"], offsetjp)
        within_range = [
            e
            for e in texten_formatted["entities"]
            if e["offset"] < 50 and e["offset"] + e["length"] < 50
        ]
        if within_range:
            entities += offset_entiries(within_range, offseten)
    else:
        offseten += compute_uft16_offset(author_section)
        text += texten
        offsetjp += compute_uft16_offset(text)
        text += textjp

        entities += offset_entiries(texten_formatted["entities"], offseten)
        entities += offset_entiries(textjp_formatted["entities"], offsetjp)

    # grok section
    for grok_response in grok_responses:
        if "extractedText" in grok_response:
            text += "\n\n🤖Grokによる画像・ミームの解説🚀\n"
            text += grok_response["explanation"] + "\n"
        else:
            text += "\n\n🤖Grokによるリンク先の解説🚀\n"
            text += grok_response

    text += footnote
    link_offset = compute_uft16_offset(text) - 2
    entities += [
        {
            "@type": "textEntity",
            "offset": link_offset,
            "length": 2,
            "type": {"@type": "textEntityTypeTextUrl", "url": link},
        }
    ]

    return text, entities


def parse_msg_translate_post(tg, msg, mock=False, use_grok=False, session=None):
    """
    parse updateMessage and translate if needed and post it to the target channel
    If it includes media, forward it first
    and then post the translated text below.
    """

    OCRParsedText = False
    send_response = None

    type_ = msg["content"]["@type"]
    msg_id = msg["id"]
    chat_id = msg["chat_id"]
    album_id = int(msg["media_album_id"])
    PostDate = datetime.fromtimestamp(msg["date"])
    texten_formatted = find_text(msg)
    texten = texten_formatted["text"]

    author, origin_channel_name, origin_author_signature = guess_authors(tg, msg)

    channel2post = channel2post_mapping[chat_id]

    # if this is the 2nd or 3rd process chasing the first process,
    # we should stop to avoid duplicates
    if album_id:
        new_album = not dbutils.get_album_id(album_id)
        print(f"It's an album!!! album_id = {album_id}")
    else:
        new_album = False

    media_post = new_album or type_ in media_message_types

    print(
        f"album? {album_id>0}, {msg_id}, {origin_channel_name} ({chat_id} {origin_author_signature}), WTM Author: {author}, Text: {texten[:40]}"
    )

    print("Getting the t.me link of the original post.")
    sleep(2)
    r = getMessageLink(tg, chat_id, msg_id, album_flag=album_id > 0)  # offline request
    r.wait()
    if r.error:
        print(r.error_info)
        link = "t.me link not generated"
        previewlink = "undefined"
    else:
        link = r.update["link"]
        previewlink = "t.me/s".join(link.split("t.me"))
        print("t.me link seems ok.", link, type_)

    grok_responses = []
    if type_ == "messagePhoto" and len(texten) < 100 and use_grok:
        grok_response = get_details_from_GrokOCR(tg, msg)
        if grok_response:
            OCRParsedText = True
            texten += f"\n\nText detected by Grok\n{grok_response['extractedText']}"
            grok_responses.append(grok_response)

    # for any posts (short comment or not)
    urls = extract_url_from_msg(msg, session)
    if use_grok and not grok_responses:
        for url in urls:
            grok_response = get_grok_summary(tg, url)
            if grok_response:
                grok_responses.append(grok_response)
            else:
                print("Failed to get a response from @GrokAI wihtin 30 sec")

    # append whatever added
    texten_formatted = {**texten_formatted, "text": texten}

    if not validate_params(msg_id, author, PostDate):
        print("Bad params:", msg_id, author, texten[:100], PostDate)

    # post it now
    if not mock:
        print(f"Post it now: {msg_id}")
        # (1) post messageLink alone for album (Bot cannot forward it https://stackoverflow.com/a/69363278)
        if new_album:
            # posting a messageLink alone has a forwarding like effect
            r = tg.send_message(channel2post, link)
            r.wait()
        # (2) forward most media/poll posts except album (especially for GIF)
        elif type_ in media_message_types and album_id == 0:
            forward_msg(tg, channel2post, msg_id, mock=False)

    # Finally any other simple text posts will be posted with translation.
    # post text with translation if it is:
    # (1) the first msg in the album
    # (2) media post but not a part of album
    # (3) or just text post
    if new_album or (type_ in media_message_types and album_id == 0) or not album_id:
        if OCRParsedText or type_ == "messagePoll":
            textjp_formatted = get_translation_telegram(
                tg, text=texten_formatted, return_formattedText=True
            )
            if not textjp_formatted:
                response = get_translation_google_translate_v2(texten)
                textjp_formatted = {
                    "@type": "formattedText",
                    "text": response,
                    "entities": [],
                }
            textjp = textjp_formatted["text"]
            entitiesjp = textjp_formatted["entities"]
        else:
            # print(f"Now call deepl API for {msg_id} by {author} on {PostDate}")
            # textjp = get_translation_each(texten, mock=False)
            sleep(6)
            textjp_formatted = get_translation_telegram(
                tg,
                msg={"chat_id": chat_id, "id": msg_id},
                return_formattedText=True,
            )
            if textjp_formatted:
                textjp = textjp_formatted.get("text", "")
                entitiesjp = textjp_formatted.get("entities", None)
            else:
                textjp = get_translation_google_translate_v2(texten)
                entitiesjp = []
                textjp_formatted = {
                    "@type": "formattedText",
                    "text": textjp,
                    "entities": entitiesjp,
                }

        if textjp and not mock:
            dbutils.insert_live_translation(msg_id, author, texten, textjp, PostDate)
        else:
            old_translation = dbutils.get_live_translation_from_db(msg_id)
            if old_translation:
                textjp = old_translation[0][3]
                entitiesjp = []
                textjp_formatted = {
                    "@type": "formattedText",
                    "text": textjp,
                    "entities": [],
                }

                print("Reusing the saved translation")

                r = tg.send_message(
                    sendErrorTo,
                    f"No translation for {link}, msg {msg_id} by {author} on {PostDate}",
                )
                r.wait()

        print("\nTranslation:\n", texten, "\n>>>\n", textjp)

        text, entities = generate_post(
            texten_formatted,
            textjp_formatted,
            author,
            origin_channel_name,
            origin_author_signature,
            PostDate,
            link,
            previewlink,
            grok_responses,
            media_post,
        )

        if mock:
            return text, entities
        else:
            send_response = tg.send_message(channel2post, text, entities=entities)
            send_response.wait()

        # add Post link of its own
        if send_response:
            if send_response.error:
                print("Error while posting:", result.error_info)
                return
            message_id = send_response.update["id"]
            print(f"Post success. message_id = {message_id}")
            sleep(1)

            hist_result = tg.get_chat_history(
                chat_id=channel2post,
                limit=2,
                from_message_id=0,
                offset=0,
                only_local=False,
            )
            hist_result.wait()
            if hist_result.error:
                print("get_chat_history failed:", hist_result.error_info)
                return

            messages = hist_result.update["messages"]
            latest_message_id = max(m["id"] for m in messages)
            rlatest = getMessageLink(
                tg, channel2post, latest_message_id, album_flag=album_id > 0
            )
            rlatest.wait()

            post_link = None
            if not rlatest.error:
                post_link = rlatest.update["link"]
                message_id = latest_message_id
                print(
                    f"Replacing the message_id {message_id} with the latest_message_id {latest_message_id} that we found post link from"
                )
            else:
                r = getMessageLink(
                    tg, channel2post, message_id, album_flag=album_id > 0
                )
                r.wait()
                if not r.error:
                    post_link = r.update["link"]
                    print(f"found post link from message_id {message_id}")

            if not post_link:
                print("link attr not found in the update from getMessageLink")
                return

            print(f"Post link of this post: {post_link}")

            link_offset = compute_uft16_offset(text)
            new_text = f"{text}🔗"

            new_entities = entities.copy()
            new_entities.append(
                {
                    "@type": "textEntity",
                    "offset": link_offset,
                    "length": 2,
                    "type": {"@type": "textEntityTypeTextUrl", "url": post_link},
                }
            )
            payload = {
                "@type": "editMessageText",
                "chat_id": channel2post,
                "message_id": message_id,
                "input_message_content": {
                    "@type": "inputMessageText",
                    "text": {
                        "@type": "formattedText",
                        "text": new_text,
                        "entities": new_entities,
                    },
                },
            }
            try:
                sleep(4)
                edit_result = tg._send_data(payload)
                edit_result.wait()

                if edit_result.error:
                    print("error while editing:", edit_result.error_info)
                else:
                    print("editing success")

            except Exception as e:
                print(f"error while editing: {type(e).__name__} - {str(e)}")

    # this should stop duplicates from album posts
    if new_album:
        dbutils.insert_album_id(album_id)


def login(VerbosityLevel=1):
    # VerbosityLevel 1 corresponds to errors,
    # value 2 corresponds to warnings and debug warnings
    API_ID = os.getenv("API_ID")
    API_HASH = os.getenv("API_HASH")
    PHONE = os.getenv("PHONE")
    DB_encryption = os.getenv("DB_encryption")
    python_version = f"{sys.version_info.major}.{sys.version_info.minor}"

    library_path = os.path.join(
        os.getenv("VIRTUAL_ENV"),
        f"lib/python{python_version}/site-packages/telegram/lib/linux/libtdjson.so",
    )

    tg = Telegram(
        api_id=API_ID,
        api_hash=API_HASH,
        phone=PHONE,
        database_encryption_key=DB_encryption,
        library_path=library_path,
        tdlib_verbosity=VerbosityLevel,
    )
    tg.login()

    return tg


if __name__ == "__main__":
    """
    Catching up with the latest post when the bot died
        1. stop the bot
        2. activate env set env vars
            GOOGLE_APPLICATION_CREDENTIALS
            PHONE
            API_ID
            API_HASH
            BOT_TOKEN
            DB_encryption
            USE_GROK
            DEPLOYMENT_STAGE
            sendErrorTo
            playground_IN
            playground_OUT
            chat_id_grok
        3. ipython
        4. import and login to get tg object
        5. get_messageFrom_tme from WTM
        6. parse_msg_translate_post one by one

    deployment:
        set env vars like 2.

        python updater.py (simple testing)
    
        or 
    
        write a bash script (nohup and capture print stdout to a log file)
    """

    tg = login()

    use_grok = os.getenv("USE_GROK") == "True"

    chats = show_chat_list(tg)
    dbutils.create_update_db()
    dbutils.create_live_translation_db()
    print(f"Starting... (USE_GROK is {use_grok})")

    # set up a session for selenium to check redirection
    session = requests.Session()
    session.headers.update(headers)

    def new_msg_hander(update):
        """
        Event handler to forward WTM posts
        """
        msg = update["message"]
        chat_id = msg["chat_id"]

        if (DEPLOYMENT_STAGE == "test" and chat_id == playground_IN) or (
            DEPLOYMENT_STAGE == "prod" and chat_id == WTM
        ):
            msg_id = msg["id"]
            if not dbutils.select_update(msg_id=msg_id):
                dbutils.insert_update(update)

            parse_msg_translate_post(
                tg, msg, mock=False, use_grok=use_grok, session=session
            )

    tg.add_message_handler(new_msg_hander)

    try:
        tg.idle()
    except KeyboardInterrupt:
        print("bot stopped")
    finally:
        session.close()
