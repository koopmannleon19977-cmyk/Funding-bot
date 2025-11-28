import os
from bs4 import BeautifulSoup
import re

# Konfiguration
INPUT_DIR = "raw_docs"
OUTPUT_FILE = "API_CONTEXT.md"

def clean_text(text):
    """Entfernt überflüssige Leerzeichen und Leerzeilen."""
    lines = (line.strip() for line in text.splitlines())
    chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
    text = '\n'.join(chunk for chunk in chunks if chunk)
    return text

def extract_content(html_content, filename):
    soup = BeautifulSoup(html_content, 'html.parser')
    
    # Versuche, den Hauptinhalt zu finden (oft in <main>, <article> oder spezifischen IDs)
    # Bei ReadMe.io (deine Dateien) ist der Content oft in bestimmten Containern.
    # Wir nehmen hier eine generische Strategie: Title + Body Text, aber filtern Navigation raus.
    
    # Entferne Skripte und Styles, um Rauschen zu reduzieren
    for script in soup(["script", "style", "nav", "footer", "header"]):
        script.decompose()

    # Titel extrahieren
    title = soup.find('title')
    title_text = title.get_text() if title else filename
    
    # Text extrahieren
    text = soup.get_text(separator='\n')
    
    # Etwas aufräumen
    clean_body = clean_text(text)
    
    # Formatierung als Markdown-Abschnitt
    return f"# SOURCE FILE: {filename}\n## {title_text}\n\n```text\n{clean_body}\n```\n\n---\n\n"

def main():
    if not os.path.exists(INPUT_DIR):
        print(f"Fehler: Ordner '{INPUT_DIR}' existiert nicht. Bitte erstellen und HTMLs reinlegen.")
        return

    all_content = "# LIGHTER / X10 API DOCUMENTATION DUMP\n\n"
    
    files = [f for f in os.listdir(INPUT_DIR) if f.endswith(".html")]
    print(f"Gefunden: {len(files)} HTML Dateien. Verarbeite...")

    for f in files:
        path = os.path.join(INPUT_DIR, f)
        try:
            with open(path, "r", encoding="utf-8") as html_file:
                content = extract_content(html_file.read(), f)
                all_content += content
                print(f"✓ {f} verarbeitet")
        except Exception as e:
            print(f"❌ Fehler bei {f}: {e}")

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(all_content)
    
    print(f"\nFERTIG! Datei '{OUTPUT_FILE}' wurde erstellt.")
    print("Du kannst den Ordner 'raw_docs' und das Script jetzt löschen.")

if __name__ == "__main__":
    main()