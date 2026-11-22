#!/usr/bin/env python3
"""
DP haftalık haber toplayıcı (Claude/API YOK, ücretsiz).
Ulusal RSS'leri, 81 ilin yerel gazetelerini (başlık bulunamazsa sitenin RSS'i) ve her il için Google Haberler RSS'ini tarar; yalnızca seçilen haftanın (Pzt-Paz)
tarihli haberlerini haberler.json'a yazar. Tarihi bulunamayan haberler ayrı listede tutulur.

Kullanım:
  python haber_topla.py                  -> içinde bulunulan/biten haftayı toplar
  python haber_topla.py --bitis 2026-11-22   -> o Pazar günü biten haftayı toplar
"""
import argparse, json, re, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse, quote
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
GOOGLE_RSS = "https://news.google.com/rss/search?q={q}&hl=tr&gl=TR&ceid=TR:tr"
GOOGLE_SORGU = '"{il}" (belediye OR başkan OR meclis OR vali OR valilik) when:7d'
RSS_YOLLARI = ["/feed", "/rss", "/rss.xml", "/feed/"]

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

def il_adi_duzgun(ad):
    k = ad.replace("İ", "i").replace("I", "ı").lower()
    return k[0].replace("i", "İ").upper() + k[1:]

def google_haberleri(il_ad):
    """Google Haberler RSS: ile göre son 7 günün haberleri (başlık, yayıncı, tarih)."""
    try:
        url = GOOGLE_RSS.format(q=quote(GOOGLE_SORGU.format(il=il_adi_duzgun(il_ad))))
        f = feedparser.parse(url, request_headers=UA)
        out = []
        for e in f.entries[:40]:
            t = e.get("published_parsed")
            d = datetime(*t[:6], tzinfo=timezone.utc).astimezone(TR).date() if t else None
            kay = (e.get("source") or {}).get("title") or ""
            out.append({"baslik": e.get("title", "").strip(), "link": e.get("link", ""),
                        "tarih": d.isoformat() if d else None, "kaynak": kay or "Google Haberler"})
        time.sleep(1.0)
        return out, (None if out else "sonuç yok")
    except Exception as ex: return [], str(ex)[:120]

def rss_bul(url, soup=None):
    """Sitenin RSS adresini sayfadaki <link rel=alternate> etiketinden ya da /feed gibi yollardan bul."""
    adaylar = []
    if soup is not None:
        for l in soup.find_all("link", attrs={"rel": "alternate"}):
            if "rss" in (l.get("type") or "") or "atom" in (l.get("type") or ""):
                if l.get("href"): adaylar.append(urljoin(url, l["href"]))
    adaylar += [urljoin(url, y) for y in RSS_YOLLARI]
    for a in dict.fromkeys(adaylar):
        h, _ = rss_oku(a)
        h = [x for x in h if x["baslik"]]
        if h:
            for x in h: x["kaynak"] = None
            return h
    return []

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
        if not adaylar:
            rs = rss_bul(g["url"], soup)
            for x in rs[:MAX_LINK]:
                x["kaynak"] = g["isim"]; haberler.append(x)
            return haberler, (None if haberler else "haber bulunamadı")
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
    g_h, g_hata = google_haberleri(il["ad"])
    return il["ad"], haberler, erisilemeyen, g_h, g_hata

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
        for ad, h, er, gh, ghata in ex.map(il_tara, iller):
            w, t, wd = ayir(h, bas, bit)
            gw, gt, gwd = ayir(gh, bas, bit)
            sonuc["iller"][ad] = {"haberler": w, "tarihsiz": t, "hafta_disi_sayisi": wd, "erisilemeyen_gazeteler": er,
                                  "google_haberleri": {"haberler": gw, "tarihsiz": gt, "hafta_disi_sayisi": gwd, "hata": ghata}}
            print(f"  {ad}: gazete {len(w)} + google {len(gw)} haber, {len(t)+len(gt)} tarihsiz, {len(er)}/3 gazete sorunlu")
    sonuc["ozet"] = {"haberli_il": sum(1 for v in sonuc["iller"].values() if v["haberler"] or v["google_haberleri"]["haberler"]),
                     "google_ile_haber_olan_il": sum(1 for v in sonuc["iller"].values() if v["google_haberleri"]["haberler"]),
                     "gazeteden_haber_olan_il": sum(1 for v in sonuc["iller"].values() if v["haberler"]),
                     "toplam_il": len(sonuc["iller"]),
                     "hic_haber_olmayan_il": [k for k, v in sonuc["iller"].items() if not v["haberler"] and not v["google_haberleri"]["haberler"]]}
    Path(a.cikti).write_text(json.dumps(sonuc, ensure_ascii=False, indent=1), encoding="utf-8")
    print("Yazıldı:", a.cikti, sonuc["ozet"])
if __name__ == "__main__": main()
