#!/usr/bin/env python3
"""
DP haftalık haber toplayıcı (Claude/API YOK, ücretsiz).
Ulusal RSS'leri ve 81 ilin yerel gazetelerini tarar; yalnızca seçilen haftanın (Pzt-Paz)
tarihli haberlerini haberler.json'a yazar. Tarihi bulunamayan haberler ayrı listede tutulur.

Kullanım:
  python haber_topla.py                  -> içinde bulunulan/biten haftayı toplar
  python haber_topla.py --bitis 2026-11-22   -> o Pazar günü biten haftayı toplar
"""
import argparse, json, re, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse
import feedparser, requests
from bs4 import BeautifulSoup

TR = timezone(timedelta(hours=3))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
      "Accept-Language": "tr-TR,tr;q=0.9"}
ULUSAL_RSS = {
    "AA": "https://www.aa.com.tr/tr/rss/default?cat=guncel", "ANKA": "https://ankahaber.net/feed",
    "Sözcü": "https://www.sozcu.com.tr/rss/gundem.xml", "Halk TV": "https://halktv.com.tr/feed",
    "CNN Türk": "https://www.cnnturk.com/feed/rss/all/news", "Habertürk": "https://www.haberturk.com/rss"}
ULUSLARARASI_RSS = {
    "BBC Türkçe": "https://feeds.bbci.co.uk/turkce/rss.xml", "Al Jazeera TR": "https://www.aljazeera.com.tr/feed",
    "Reuters TR": "https://tr.reuters.com/rssFeed/topNews"}
BASLIK_SEC = ["h1 a", "h2 a", "h3 a", ".news-title a", ".haber-baslik a", ".title a", "article a"]
MAX_LINK, ESZAMANLI, ZAMAN_ASIMI = 8, 8, 12

def hafta_araligi(bitis_str=None):
    if bitis_str: bitis = datetime.strptime(bitis_str, "%Y-%m-%d").date()
    else:
        bugun = datetime.now(TR).date()
        bitis = bugun if bugun.weekday() == 6 else bugun - timedelta(days=bugun.weekday() + 1)
    return bitis - timedelta(days=6), bitis

def tarih_coz(s):
    if not s: return None
    s = str(s).strip()
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return (d if d.tzinfo else d.replace(tzinfo=TR)).astimezone(TR).date()
    except Exception: pass
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m: return datetime(int(m[1]), int(m[2]), int(m[3])).date()
    m = re.match(r"(\d{1,2})[./](\d{1,2})[./](\d{4})", s)
    if m: return datetime(int(m[3]), int(m[2]), int(m[1])).date()
    return None

def sayfa_tarihi(html):
    """Makale sayfasından yayın tarihini çıkar: meta, JSON-LD, <time>."""
    soup = BeautifulSoup(html, "html.parser")
    for prop in ("article:published_time", "og:published_time", "datePublished", "pubdate", "publishdate", "date"):
        el = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop}) or soup.find("meta", attrs={"itemprop": prop})
        if el and el.get("content"):
            d = tarih_coz(el["content"])
            if d: return d
    for sc in soup.find_all("script", type="application/ld+json"):
        m = re.search(r'"datePublished"\s*:\s*"([^"]+)"', sc.string or "")
        if m:
            d = tarih_coz(m[1])
            if d: return d
    t = soup.find("time")
    if t:
        d = tarih_coz(t.get("datetime") or t.get_text(strip=True))
        if d: return d
    return None

def al(url):
    r = requests.get(url, headers=UA, timeout=ZAMAN_ASIMI)
    r.raise_for_status()
    if not r.encoding or r.encoding.lower() == "iso-8859-1": r.encoding = r.apparent_encoding
    return r.text

def rss_oku(url):
    try:
        f = feedparser.parse(url, request_headers=UA)
        out = []
        for e in f.entries[:60]:
            t = e.get("published_parsed") or e.get("updated_parsed")
            d = datetime(*t[:6], tzinfo=timezone.utc).astimezone(TR).date() if t else None
            out.append({"baslik": e.get("title", "").strip(), "link": e.get("link", ""), "tarih": d.isoformat() if d else None,
                        "ozet": BeautifulSoup(e.get("summary", ""), "html.parser").get_text(" ", strip=True)[:300]})
        return out, None
    except Exception as ex: return [], str(ex)[:120]

def gazete_tara(g):
    """Ana sayfadan başlık+link topla, her linkin sayfasından tarihi oku."""
    haberler, hata = [], None
    try:
        soup = BeautifulSoup(al(g["url"]), "html.parser"); gorulen = set(); adaylar = []
        for sel in BASLIK_SEC:
            for a in soup.select(sel):
                b = a.get_text(" ", strip=True); h = a.get("href")
                if not h or len(b) < 20: continue
                link = urljoin(g["url"], h)
                if urlparse(link).netloc.replace("www.", "") != urlparse(g["url"]).netloc.replace("www.", ""): continue
                if link in gorulen or link.rstrip("/") == g["url"].rstrip("/"): continue
                gorulen.add(link); adaylar.append((b, link))
            if len(adaylar) >= MAX_LINK: break
        for b, link in adaylar[:MAX_LINK]:
            d = None
            try: d = sayfa_tarihi(al(link))
            except Exception: pass
            haberler.append({"baslik": b, "link": link, "tarih": d.isoformat() if d else None, "kaynak": g["isim"]})
            time.sleep(0.3)
    except Exception as ex: hata = str(ex)[:120]
    return haberler, hata

def il_tara(il):
    haberler, erisilemeyen = [], []
    for g in il["gazeteler"]:
        h, hata = gazete_tara(g)
        if hata or not h: erisilemeyen.append({"gazete": g["isim"], "url": g["url"], "hata": hata or "haber bulunamadı"})
        haberler += h
    return il["ad"], haberler, erisilemeyen

def ayir(haberler, bas, bit):
    haftalik, tarihsiz, hafta_disi = [], [], 0
    for h in haberler:
        d = tarih_coz(h.get("tarih"))
        if d is None: tarihsiz.append(h)
        elif bas <= d <= bit: haftalik.append(h)
        else: hafta_disi += 1
    return haftalik, tarihsiz, hafta_disi

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--bitis"); ap.add_argument("--cikti", default="haberler.json")
    a = ap.parse_args(); bas, bit = hafta_araligi(a.bitis)
    print(f"Hafta: {bas} - {bit}")
    sonuc = {"hafta_baslangic": bas.isoformat(), "hafta_bitis": bit.isoformat(),
             "uretim_zamani": datetime.now(TR).isoformat(timespec="seconds"),
             "not": "Yalnızca tarihi doğrulanan haberler 'haberler' listesindedir; tarihi bulunamayanlar 'tarihsiz'dedir ve doğrulanmadan kullanılmamalıdır.",
             "ulusal": {}, "iller": {}, "ozet": {}}
    for kaynak, url in {**ULUSAL_RSS, **ULUSLARARASI_RSS}.items():
        h, hata = rss_oku(url); w, t, wd = ayir(h, bas, bit)
        sonuc["ulusal"][kaynak] = {"haberler": w, "tarihsiz": t, "hafta_disi_sayisi": wd, "hata": hata}
        print(f"  {kaynak}: {len(w)} haber" + (f" (HATA {hata})" if hata else ""))
    iller = json.load(open(Path(__file__).with_name("iller.json"), encoding="utf-8"))
    with ThreadPoolExecutor(ESZAMANLI) as ex:
        for ad, h, er in ex.map(il_tara, iller):
            w, t, wd = ayir(h, bas, bit)
            sonuc["iller"][ad] = {"haberler": w, "tarihsiz": t, "hafta_disi_sayisi": wd, "erisilemeyen_gazeteler": er}
            print(f"  {ad}: {len(w)} haber, {len(t)} tarihsiz, {len(er)}/3 gazete sorunlu")
    sonuc["ozet"] = {"haberli_il": sum(1 for v in sonuc["iller"].values() if v["haberler"]),
                     "toplam_il": len(sonuc["iller"]),
                     "tamamen_erisilemeyen_il": [k for k, v in sonuc["iller"].items() if len(v["erisilemeyen_gazeteler"]) == 3]}
    Path(a.cikti).write_text(json.dumps(sonuc, ensure_ascii=False, indent=1), encoding="utf-8")
    print("Yazıldı:", a.cikti, sonuc["ozet"]["haberli_il"], "/", sonuc["ozet"]["toplam_il"], "ilde haftalık haber")
if __name__ == "__main__": main()
