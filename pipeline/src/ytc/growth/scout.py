"""The scout: festivals coming up, and the Shorts drawing the most views in the brand's niche this week.

Festival dates are for Delhi, checked against several panchang calendars on 17 Oct 2026 (simplyastro.com,
samvat.in, drrpsharma.com, desi.net); extend the list before April 2027. The niche search costs about 600 of the
brand project's 10,000 daily YouTube API units, so it runs once a day.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

from .ankahi import FAMOUS_HI

log = logging.getLogger(__name__)

# (date, festival, in Hindi, stories from the three texts that fit it)
FESTIVALS = (
    ("2026-10-19", "Durga Ashtami", "दुर्गा अष्टमी", "Durga and the goddess's forms"),
    ("2026-10-20", "Dussehra (Vijayadashami)", "दशहरा", "Rama's victory over Ravana (Yuddha Kanda); the Pandavas "
     "take back their weapons from the Shami tree (Virata Parva)"),
    ("2026-10-25", "Sharad Purnima", "शरद पूर्णिमा", "Krishna's rasa on the autumn full moon (Bhagavata 10.29-33)"),
    ("2026-10-29", "Karva Chauth", "करवा चौथ", "a wife's devotion: Savitri and Satyavan (Vana Parva)"),
    ("2026-11-01", "Ahoi Ashtami", "अहोई अष्टमी", "mothers and children: Yashoda and Krishna, Kunti"),
    ("2026-11-06", "Dhanteras", "धनतेरस", "Dhanvantari rises from the churning of the ocean (Bhagavata 8.8)"),
    ("2026-11-07", "Naraka Chaturdashi (Choti Diwali)", "नरक चतुर्दशी", "Krishna and Satyabhama defeat Narakasura "
     "and free his captives (Bhagavata 10.59)"),
    ("2026-11-08", "Diwali", "दीपावली", "Rama comes home to Ayodhya (Yuddha Kanda); Lakshmi"),
    ("2026-11-10", "Govardhan Puja", "गोवर्धन पूजा", "Krishna lifts Govardhan (Bhagavata 10.24-27)"),
    ("2026-11-11", "Bhai Dooj", "भाई दूज", "brothers and sisters: Krishna and Subhadra, Yama and Yamuna"),
    ("2026-11-15", "Chhath Puja", "छठ पूजा", "Surya: Karna, Surya's son; Kunti's mantra (Adi and Vana Parva)"),
    ("2026-11-20", "Dev Uthani Ekadashi", "देवउठनी एकादशी", "Vishnu wakes; Ambarisha's Ekadashi vow (Bhagavata 9.4-5)"),
    ("2026-11-21", "Tulsi Vivah", "तुलसी विवाह", "Tulsi and Vishnu"),
    ("2026-11-24", "Kartik Purnima (Dev Deepawali)", "कार्तिक पूर्णिमा", "Shiva destroys the three cities of Tripura "
     "(Karna Parva); Vishnu's Matsya avatar (Bhagavata 8.24)"),
    ("2026-12-14", "Vivah Panchami", "विवाह पंचमी", "Rama and Sita's wedding (Bala Kanda)"),
    ("2026-12-20", "Gita Jayanti", "गीता जयंती", "Krishna's counsel to Arjuna (Bhishma Parva)"),
    ("2026-12-23", "Dattatreya Jayanti", "दत्तात्रेय जयंती", "Dattatreya, son of Atri and Anasuya, and his 24 teachers "
     "(Bhagavata 4.1, 11.7-9)"),
    ("2027-01-15", "Makar Sankranti", "मकर संक्रांति", "Bhishma waits on his bed of arrows for the sun's northward "
     "turn (Anusasana Parva)"),
    ("2027-02-11", "Vasant Panchami", "वसंत पंचमी", "Saraswati"),
    ("2027-03-06", "Maha Shivaratri", "महाशिवरात्रि", "Shiva drinks the poison (Bhagavata 8.7); Arjuna and the "
     "hunter Shiva (Vana Parva)"),
    ("2027-03-21", "Holika Dahan", "होलिका दहन", "Prahlada, Hiranyakashipu and Narasimha (Bhagavata 7)"),
    ("2027-03-22", "Holi", "होली", "Krishna in Vrindavan; Prahlada"),
    ("2027-04-15", "Ram Navami", "राम नवमी", "Rama's birth (Bala Kanda 18)"),
    ("2027-04-20", "Hanuman Jayanti", "हनुमान जयंती", "Hanuman's birth and childhood (Kishkindha Kanda 66)"),
)


def festivals(today: date, days: int = 50) -> list[dict]:
    out = []
    for day, name, hindi, stories in FESTIVALS:
        left = (date.fromisoformat(day) - today).days
        if 0 <= left <= days:
            out.append({"date": day, "festival": name, "hindi": hindi, "days_left": left, "stories": stories})
    return out


def niche(token_file: Path, queries: tuple[str, ...], own_channel: str, now: datetime, days: int = 10) -> list[dict]:
    """The most-viewed Shorts of the last `days` days for each search, Hindi and India, most views first."""
    from googleapiclient.discovery import build

    from .. import youtube

    data = build("youtube", "v3", credentials=youtube._credentials(token_file), cache_discovery=False)
    after = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    found: dict[str, dict] = {}
    for query in queries:
        try:
            result = data.search().list(part="snippet", q=query, type="video", videoDuration="short", order="viewCount",
                                        publishedAfter=after, regionCode="IN", relevanceLanguage="hi",
                                        maxResults=12).execute()
            ids = [item["id"]["videoId"] for item in result.get("items", [])
                   if item["snippet"].get("channelId") != own_channel]
            if not ids:
                continue
            for video in data.videos().list(part="snippet,statistics", id=",".join(ids)).execute().get("items", []):
                found.setdefault(video["id"], {
                    "title": video["snippet"]["title"][:120], "channel": video["snippet"]["channelTitle"][:60],
                    "views": int(video["statistics"].get("viewCount", 0)),
                    "published": video["snippet"]["publishedAt"][:10], "query": query,
                })
        except Exception as err:
            log.warning("niche search %r failed: %s", query, err)
    return sorted(found.values(), key=lambda v: -v["views"])[:30]


def figures_in(titles: list[str]) -> dict[str, int]:
    """How often each well-known figure is named in these titles."""
    counts = Counter(name for title in titles for name in FAMOUS_HI if name in title)
    return dict(counts.most_common(10))
