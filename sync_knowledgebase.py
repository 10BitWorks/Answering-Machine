import urllib.request
import json
import ssl
import os
import re
import sys
from markdownify import markdownify as md

# Setup SSL context to ignore cert errors (as seen in existing scripts)
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

TOKEN = os.getenv("ZAMMAD_API_TOKEN")
if not TOKEN:
    print("Error: ZAMMAD_API_TOKEN environment variable is not set.")
    sys.exit(1)

BASE_URL = "https://support.10bitworks.org/api/v1"
OUTPUT_DIR = "./knowledgebase"

headers = {
    "Authorization": f"Token token={TOKEN}",
    "Content-Type": "application/json"
}

def fetch_json(url):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, context=ctx) as response:
        return json.loads(response.read().decode('utf-8'))

def sanitize_filename(filename):
    return re.sub(r'[^A-Za-z0-9._-]', '_', filename)

def main():
    if not os.path.exists(OUTPUT_DIR):
        os.makedirs(OUTPUT_DIR)

    print("Fetching Knowledge Base 1...")
    kb_url = f"{BASE_URL}/knowledge_bases/1?include_contents=1&full=1&expand=true"
    try:
        kb_data = fetch_json(kb_url)
    except Exception as e:
        print(f"Error fetching KB: {e}")
        return

    answer_ids = kb_data.get('answer_ids', [])
    if not answer_ids:
        print("No answer IDs found.")
        return

    written_files = []
    
    for aid in answer_ids:
        print(f"Fetching Answer ID: {aid}...")
        answer_url = f"{BASE_URL}/knowledge_bases/1/answers/{aid}?include_contents={aid}"
        try:
            answer_data = fetch_json(answer_url)
            
            assets = answer_data.get('assets', {})
            
            # Extract Title
            translations = assets.get('KnowledgeBaseAnswerTranslation', {})
            title = "Unknown_Title"
            if translations:
                first_key = list(translations.keys())[0]
                title = translations[first_key].get('title', "Unknown_Title")
            
            # Extract Category
            categories = assets.get('KnowledgeBaseCategoryTranslation', {})
            category = "Uncategorized"
            if categories:
                first_key = list(categories.keys())[0]
                category = categories[first_key].get('title', "Uncategorized")
                
            # Extract Body
            contents = assets.get('KnowledgeBaseAnswerTranslationContent', {})
            body_html = ""
            if contents:
                first_key = list(contents.keys())[0]
                body_html = contents[first_key].get('body', "")

            # Convert HTML to Markdown
            body_md = md(body_html, heading_style="ATX")

            safe_title = sanitize_filename(title)
            if not safe_title or safe_title == "_":
                safe_title = f"Answer_{aid}"
            
            filename = f"{safe_title}.md"
            file_path = os.path.join(OUTPUT_DIR, filename)
            
            with open(file_path, "w", encoding="utf-8") as f:
                f.write("---\n")
                f.write(f'title: "{title}"\n')
                f.write(f'category: "{category}"\n')
                f.write(f'answer_id: {aid}\n')
                f.write("---\n\n")
                f.write(body_md)
            
            written_files.append(filename)
            print(f"Saved {file_path}")
            
        except Exception as e:
            print(f"Error fetching answer {aid}: {e}")

    # Check for sync success or fatal failure
    existing_md_files = [f for f in os.listdir(OUTPUT_DIR) if f.endswith(".md")]
    
    if len(written_files) == 0:
        if len(existing_md_files) == 0:
            raise RuntimeError("Fatal error: Failed to sync any knowledge base articles and no local cache exists.")
        else:
            print("Warning: Failed to sync any knowledge base articles, but a local cache exists. Retaining stale files.")
    else:
        # Cleanup stale files that were deleted in Zammad
        for f in existing_md_files:
            if f not in written_files:
                stale_path = os.path.join(OUTPUT_DIR, f)
                os.remove(stale_path)
                print(f"Deleted stale file: {stale_path}")

if __name__ == "__main__":
    main()
