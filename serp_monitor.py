import requests
from bs4 import BeautifulSoup
import pandas as pd
import json
import logging
from datetime import datetime
import time
import os
from pathlib import Path
import re
import schedule
from dotenv import load_dotenv
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication

# Carica variabili ambiente
load_dotenv()

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('data/serp_monitor.log'),
        logging.StreamHandler()
    ]
)

# Configurazione
DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

EXCEL_FILE = DATA_DIR / "serp_monitoring_results.xlsx"
HISTORY_FILE = DATA_DIR / "serp_history.json"

# Keywords da monitorare
KEYWORDS = [
    "Leonardo Apache La Russa",
    "Leonardo La Russa",
    "Apache La Russa",
    "Leonardo Apache",
    "La Russa Leonardo",
    "Apache Leonardo La Russa",
    "Leonardo La Russa news"
]

# Sentiment keywords
NEGATIVE_KEYWORDS = [
    'scandalo', 'critica', 'polemiche', 'accusa', 'condanna', 'fallimento',
    'disastro', 'problema', 'errore', 'bufera', 'caso', 'inchiesta',
    'denunciato', 'arrestato', 'indagato', 'controversia', 'smentita'
]

POSITIVE_KEYWORDS = [
    'successo', 'eccellenza', 'premio', 'vittoria', 'innovazione', 'trionfo',
    'riconoscimento', 'leadership', 'merito', 'onore', 'apprezzamento',
    'elogio', 'fiducia', 'stima', 'prestigio'
]

# Headers per evitare blocchi
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7',
    'Accept-Encoding': 'gzip, deflate, br',
    'DNT': '1',
    'Connection': 'keep-alive',
    'Upgrade-Insecure-Requests': '1'
}

def search_google(keyword, num_results=10, time_filter=None):
    """Cerca su Google e restituisce i risultati
    
    Args:
        keyword: parola chiave da cercare
        num_results: numero di risultati da recuperare
        time_filter: filtro temporale ('day', 'week', 'month', None)
    """
    
    # Prova prima con SerpAPI se disponibile
    serpapi_key = os.getenv('SERPAPI_KEY')
    if serpapi_key:
        try:
            logging.info(f"🔍 Cercando su Google (SerpAPI): {keyword}" + 
                        (f" [filtro: {time_filter}]" if time_filter else ""))
            
            params = {
                'engine': 'google',
                'q': keyword,
                'num': num_results,
                'hl': 'it',
                'gl': 'it',
                'api_key': serpapi_key
            }
            
            # Aggiungi filtro temporale se specificato
            if time_filter == 'day':
                params['tbs'] = 'qdr:d'  # ultime 24 ore
            elif time_filter == 'week':
                params['tbs'] = 'qdr:w'  # ultima settimana
            elif time_filter == 'month':
                params['tbs'] = 'qdr:m'  # ultimo mese
            
            response = requests.get('https://serpapi.com/search', params=params, timeout=15)
            response.raise_for_status()
            data = response.json()
            
            results = []
            organic_results = data.get('organic_results', [])
            
            for idx, item in enumerate(organic_results[:num_results], 1):
                results.append({
                    'position': idx,
                    'title': item.get('title', 'N/A'),
                    'url': item.get('link', 'N/A'),
                    'snippet': item.get('snippet', ''),
                    'source': 'Google'
                })
            
            if results:
                logging.info(f"✓ Trovati {len(results)} risultati Google per '{keyword}'")
            else:
                logging.warning(f"⚠ Nessun risultato Google trovato per '{keyword}'")
            
            return results
            
        except Exception as e:
            logging.error(f"✗ Errore SerpAPI Google: {e}")
            logging.info("→ Tento con scraping diretto...")
    
    # Fallback: scraping diretto (senza filtro temporale)
    try:
        logging.info(f"🔍 Cercando su Google (scraping): {keyword}")
        
        url = f"https://www.google.com/search?q={keyword}&num={num_results}&hl=it"
        time.sleep(3)
        
        response = requests.get(url, headers=HEADERS, timeout=10)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, 'html.parser')
        results = []
        
        search_results = soup.find_all('div', class_='g')
        
        for idx, result in enumerate(search_results[:num_results], 1):
            try:
                title_elem = result.find('h3')
                title = title_elem.get_text() if title_elem else 'N/A'
                
                link_elem = result.find('a')
                link = link_elem['href'] if link_elem and 'href' in link_elem.attrs else 'N/A'
                
                snippet_elem = result.find('div', class_=['VwiC3b', 'yXK7lf'])
                snippet = snippet_elem.get_text() if snippet_elem else ''
                
                if title != 'N/A':
                    results.append({
                        'position': idx,
                        'title': title,
                        'url': link,
                        'snippet': snippet,
                        'source': 'Google'
                    })
            except Exception as e:
                logging.warning(f"Errore parsing risultato {idx}: {e}")
                continue
        
        if results:
            logging.info(f"✓ Trovati {len(results)} risultati Google per '{keyword}'")
        else:
            logging.warning(f"⚠ Nessun risultato Google trovato per '{keyword}'")
        
        return results
        
    except Exception as e:
        logging.error(f"✗ Errore ricerca Google per '{keyword}': {e}")
        return []

def search_bing(keyword, num_results=10, time_filter=None):
    """Cerca su Bing e restituisce i risultati
    
    Args:
        keyword: parola chiave da cercare
        num_results: numero di risultati da recuperare
        time_filter: filtro temporale ('day', 'week', 'month', None)
    """
    
    # Prova prima con SerpAPI se disponibile
    serpapi_key = os.getenv('SERPAPI_KEY')
    if serpapi_key:
        try:
            logging.info(f"🔍 Cercando su Bing (SerpAPI): {keyword}" +
                        (f" [filtro: {time_filter}]" if time_filter else ""))
            
            params = {
                'engine': 'bing',
                'q': keyword,
                'count': num_results,
                'cc': 'it',
                'api_key': serpapi_key
            }
            
            # Aggiungi filtro temporale se specificato
            if time_filter == 'day':
                params['qft'] = 'interval%3d%221%22'  # ultime 24 ore
            elif time_filter == 'week':
                params['qft'] = 'interval%3d%227%22'  # ultima settimana
            elif time_filter == 'month':
                params['qft'] = 'interval%3d%2230%22'  # ultimo mese
            
            response = requests.get('https://serpapi.com/search', params=params, timeout=15)
            response.raise_for_status()
            data = response.json()
            
            results = []
            organic_results = data.get('organic_results', [])
            
            for idx, item in enumerate(organic_results[:num_results], 1):
                results.append({
                    'position': idx,
                    'title': item.get('title', 'N/A'),
                    'url': item.get('link', 'N/A'),
                    'snippet': item.get('snippet', ''),
                    'source': 'Bing'
                })
            
            if results:
                logging.info(f"✓ Trovati {len(results)} risultati Bing per '{keyword}'")
            else:
                logging.warning(f"⚠ Nessun risultato Bing trovato per '{keyword}'")
            
            return results
            
        except Exception as e:
            logging.error(f"✗ Errore SerpAPI Bing: {e}")
            logging.info("→ Tento con scraping diretto...")
    
    # Fallback: scraping diretto (senza filtro temporale)
    try:
        logging.info(f"🔍 Cercando su Bing (scraping): {keyword}")
        
        url = f"https://www.bing.com/search?q={keyword}&count={num_results}"
        time.sleep(3)
        
        response = requests.get(url, headers=HEADERS, timeout=10)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, 'html.parser')
        results = []
        
        search_results = soup.find_all('li', class_='b_algo')
        
        for idx, result in enumerate(search_results[:num_results], 1):
            try:
                title_elem = result.find('h2')
                title = title_elem.get_text() if title_elem else 'N/A'
                
                link_elem = result.find('a')
                link = link_elem['href'] if link_elem and 'href' in link_elem.attrs else 'N/A'
                
                snippet_elem = result.find('p')
                snippet = snippet_elem.get_text() if snippet_elem else ''
                
                if title != 'N/A':
                    results.append({
                        'position': idx,
                        'title': title,
                        'url': link,
                        'snippet': snippet,
                        'source': 'Bing'
                    })
            except Exception as e:
                logging.warning(f"Errore parsing risultato Bing {idx}: {e}")
                continue
        
        if results:
            logging.info(f"✓ Trovati {len(results)} risultati Bing per '{keyword}'")
        else:
            logging.warning(f"⚠ Nessun risultato Bing trovato per '{keyword}'")
        
        return results
        
    except Exception as e:
        logging.error(f"✗ Errore ricerca Bing per '{keyword}': {e}")
        return []

def analyze_sentiment(text):
    """Analizza il sentiment di un testo"""
    text_lower = text.lower()
    
    negative_count = sum(1 for word in NEGATIVE_KEYWORDS if word in text_lower)
    positive_count = sum(1 for word in POSITIVE_KEYWORDS if word in text_lower)
    
    if negative_count > positive_count:
        return 'NEGATIVO'
    elif positive_count > negative_count:
        return 'POSITIVO'
    else:
        return 'NEUTRO'

def calculate_reputation_score(results):
    """Calcola score reputazione basato sui top 5 risultati"""
    if not results:
        return 0
    
    top_5 = results[:5]
    
    positive = sum(1 for r in top_5 if r.get('sentiment') == 'POSITIVO')
    negative = sum(1 for r in top_5 if r.get('sentiment') == 'NEGATIVO')
    neutral = sum(1 for r in top_5 if r.get('sentiment') == 'NEUTRO')
    
    # Score: positivi danno +20 punti, neutri +10, negativi -15
    score = (positive * 20) + (neutral * 10) - (negative * 15)
    
    # Normalizza 0-100
    score = max(0, min(100, score + 50))
    
    return round(score, 1)

def analyze_keywords():
    """Analizza tutte le keywords e genera report"""
    
    logging.info("=" * 60)
    logging.info(f"SERP Monitoring Report - {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    logging.info("=" * 60)
    
    all_results = []
    summary_data = []
    
    for idx, keyword in enumerate(KEYWORDS, 1):
        logging.info(f"[{idx}/{len(KEYWORDS)}] Analizzando: {keyword}")
        
        # Cerca su Google
        google_results = search_google(keyword)
        
        # Cerca su Bing
        bing_results = search_bing(keyword)
        
        # Combina risultati
        combined_results = google_results + bing_results
        
        # Analizza sentiment per ogni risultato
        for result in combined_results:
            full_text = f"{result['title']} {result['snippet']}"
            result['sentiment'] = analyze_sentiment(full_text)
            result['keyword'] = keyword
            result['timestamp'] = datetime.now().isoformat()
        
        all_results.extend(combined_results)
        
        # Calcola metriche per questa keyword
        google_only = [r for r in combined_results if r['source'] == 'Google']
        bing_only = [r for r in combined_results if r['source'] == 'Bing']
        
        google_score = calculate_reputation_score(google_only)
        bing_score = calculate_reputation_score(bing_only)
        avg_score = (google_score + bing_score) / 2 if google_only and bing_only else (google_score or bing_score)
        
        google_top5_pos = sum(1 for r in google_only[:5] if r.get('sentiment') == 'POSITIVO')
        google_top5_neg = sum(1 for r in google_only[:5] if r.get('sentiment') == 'NEGATIVO')
        
        bing_top5_pos = sum(1 for r in bing_only[:5] if r.get('sentiment') == 'POSITIVO')
        bing_top5_neg = sum(1 for r in bing_only[:5] if r.get('sentiment') == 'NEGATIVO')
        
        status = '✓ OK' if avg_score >= 70 else ('⚠ ATTENZIONE' if avg_score >= 40 else '❌ CRITICO')
        
        summary_data.append({
            'Keyword': keyword,
            'Risultati Google': len(google_only),
            'Risultati Bing': len(bing_only),
            'Google Score': google_score,
            'Bing Score': bing_score,
            'Score Medio': round(avg_score, 1),
            'Google Top5 Positivi': google_top5_pos,
            'Google Top5 Negativi': google_top5_neg,
            'Bing Top5 Positivi': bing_top5_pos,
            'Bing Top5 Negativi': bing_top5_neg,
            'Stato': status,
            'Timestamp': datetime.now().isoformat(),
            'google_results': google_only,  # Salva i risultati per l'email
            'bing_results': bing_only
        })
        
        # Log risultati
        logging.info(f"  Google: {len(google_only)} risultati, Score: {google_score}/100")
        logging.info(f"  Bing: {len(bing_only)} risultati, Score: {bing_score}/100")
        logging.info(f"  → Score Medio: {round(avg_score, 1)}/100")
        logging.info(f"  → Stato: {status}")
    
    # Salva risultati (SEMPRE, anche se vuoti)
    save_results(all_results, summary_data)
    
    # Salva storico
    save_history(summary_data)
    
    logging.info("=" * 60)
    if all_results:
        logging.info(f"✓ Analisi completata. {len(all_results)} risultati totali salvati in {EXCEL_FILE}")
    else:
        logging.info(f"⚠ Analisi completata. Nessun risultato trovato, ma file Excel creato in {EXCEL_FILE}")
    logging.info("=" * 60)
    
    # Invia email se configurato
    if os.getenv('ALERT_EMAIL'):
        send_email_report(summary_data)
    
    return summary_data

def save_results(results, summary):
    """Salva i risultati in Excel con formato specifico"""
    
    try:
        with pd.ExcelWriter(EXCEL_FILE, engine='openpyxl') as writer:
            
            # FOGLIO 1: Dettaglio SERP
            if results:
                df_details = pd.DataFrame(results)
                df_details = df_details[['keyword', 'source', 'position', 'title', 'url', 'snippet', 'timestamp']]
                df_details.columns = ['Keyword', 'Motore', 'Posizione', 'Titolo', 'URL', 'Snippet', 'Timestamp']
            else:
                df_details = pd.DataFrame(columns=['Keyword', 'Motore', 'Posizione', 'Titolo', 'URL', 'Snippet', 'Timestamp'])
            
            df_details.to_excel(writer, sheet_name='Dettaglio SERP', index=False)
            
            # FOGLIO 2: Summary
            if summary:
                summary_simple = []
                for item in summary:
                    summary_simple.append({
                        'Keyword': item['Keyword'],
                        'Risultati Google': item['Risultati Google'],
                        'Risultati Bing': item['Risultati Bing'],
                        'Timestamp': item['Timestamp']
                    })
                df_summary = pd.DataFrame(summary_simple)
            else:
                df_summary = pd.DataFrame(columns=['Keyword', 'Risultati Google', 'Risultati Bing', 'Timestamp'])
            
            df_summary.to_excel(writer, sheet_name='Summary', index=False)
            
            # FOGLIO 3: Statistiche
            total_google = sum(item['Risultati Google'] for item in summary) if summary else 0
            total_bing = sum(item['Risultati Bing'] for item in summary) if summary else 0
            
            # Calcola posizione media (dalla lista results)
            if results:
                avg_position = sum(r['position'] for r in results) / len(results)
            else:
                avg_position = 0.0
            
            stats = {
                'Metrica': [
                    'Keywords Totali',
                    'Notizie Google raccolte',
                    'Notizie Bing raccolte',
                    'Posizione media SERP',
                    'Ultima Analisi'
                ],
                'Valore': [
                    len(summary) if summary else 0,
                    f"{total_google}",
                    f"{total_bing}",
                    f"{avg_position:.1f}",
                    datetime.now().isoformat()
                ]
            }
            
            df_stats = pd.DataFrame(stats)
            df_stats.to_excel(writer, sheet_name='Statistiche', index=False)
        
        logging.info(f"✓ File Excel salvato: {EXCEL_FILE}")
        
    except Exception as e:
        logging.error(f"✗ Errore salvataggio Excel: {e}")

def save_history(summary):
    """Salva lo storico delle analisi"""
    try:
        history = []
        if HISTORY_FILE.exists():
            with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
                history = json.load(f)
        
        history.append({
            'timestamp': datetime.now().isoformat(),
            'summary': summary
        })
        
        # Mantieni solo ultimi 90 giorni
        history = history[-90:]
        
        with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
        
        logging.info(f"✓ Storico aggiornato: {HISTORY_FILE}")
        
    except Exception as e:
        logging.error(f"✗ Errore salvataggio storico: {e}")

def send_email_report(summary):
    """Invia report via email se configurato"""
    try:
        sender = os.getenv('SENDER_EMAIL')
        password = os.getenv('SENDER_PASSWORD')
        recipient = os.getenv('ALERT_EMAIL')
        
        if not all([sender, password, recipient]):
            logging.info("ℹ Email non configurata, skip invio")
            return
        
        # Crea email HTML
        msg = MIMEMultipart()
        msg['From'] = sender
        msg['To'] = recipient
        msg['Subject'] = f"SERP Monitoring Report - {datetime.now().strftime('%d/%m/%Y %H:%M')}"
        
        # Body HTML con link alle notizie reali trovate
        html = """
        <html>
          <body style="font-family: Arial, sans-serif;">
            <h2>Report SERP Monitoring</h2>
        """
        
        # Per ogni keyword, mostra i link reali alle notizie
        for item in summary:
            keyword = item['Keyword']
            html += f"<p><strong>Keyword: {keyword}</strong><br>"
            
            # Link Google - primi 3 risultati reali
            google_results = item.get('google_results', [])
            if google_results:
                html += "<strong>Link Google:</strong><br>"
                for idx, result in enumerate(google_results[:3], 1):
                    if result['url'] != 'N/A':
                        html += f'{idx}. <a href="{result["url"]}">{result["title"]}</a><br>'
            else:
                html += "<strong>Link Google:</strong> Nessun risultato trovato<br>"
            
            # Link Bing - primi 3 risultati reali
            bing_results = item.get('bing_results', [])
            if bing_results:
                html += "<strong>Link Bing:</strong><br>"
                for idx, result in enumerate(bing_results[:3], 1):
                    if result['url'] != 'N/A':
                        html += f'{idx}. <a href="{result["url"]}">{result["title"]}</a><br>'
            else:
                html += "<strong>Link Bing:</strong> Nessun risultato trovato<br>"
            
            html += "</p>"
        
        html += """
          </body>
        </html>
        """
        
        msg.attach(MIMEText(html, 'html'))
        
        # Allega Excel
        if EXCEL_FILE.exists():
            with open(EXCEL_FILE, 'rb') as f:
                attach = MIMEApplication(f.read(), _subtype='xlsx')
                attach.add_header('Content-Disposition', 'attachment', filename=EXCEL_FILE.name)
                msg.attach(attach)
        
        # Invia
        with smtplib.SMTP(os.getenv('SMTP_SERVER', 'smtp.gmail.com'), int(os.getenv('SMTP_PORT', 587))) as server:
            server.starttls()
            server.login(sender, password)
            server.send_message(msg)
        
        logging.info(f"✓ Email inviata a {recipient}")
        
    except Exception as e:
        logging.error(f"✗ Errore invio email: {e}")

def run_scheduler():
    """Esegue lo scheduler per analisi periodiche"""
    logging.info("🚀 Scheduler avviato")
    logging.info("📅 Analisi giornaliera: 09:00")
    logging.info("📊 Report settimanale: Domenica 18:00")
    logging.info("")
    
    # Analisi giornaliera alle 09:00
    schedule.every().day.at("09:00").do(analyze_keywords)
    
    # Report settimanale domenica alle 18:00
    schedule.every().sunday.at("18:00").do(analyze_keywords)
    
    # Esegui subito una volta all'avvio
    logging.info("🏃 Esecuzione analisi iniziale...")
    analyze_keywords()
    
    # Loop infinito
    while True:
        schedule.run_pending()
        time.sleep(60)

if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1 and sys.argv[1] == '--schedule':
        run_scheduler()
    else:
        analyze_keywords()