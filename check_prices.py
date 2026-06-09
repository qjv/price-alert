import csv
import json
import os
import re
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

import requests


ALERTS_FILE = Path("alerts.csv")
STATE_FILE = Path("state.json")


def brl_to_float(value: str) -> float:
    return float(value.replace(".", "").replace(",", "."))


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

def fetch_html(url):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) "
            "Gecko/20100101 Firefox/126.0"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.7,en;q=0.6",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Referer": "https://www.tudocelular.com/",
        "DNT": "1",
    }

    proxy_url = os.environ.get("HTTP_PROXY_URL")

    proxies = None
    if proxy_url:
        proxies = {
            "http": proxy_url,
            "https": proxy_url,
        }

    response = requests.get(
        url,
        headers=headers,
        timeout=30,
        allow_redirects=True,
        proxies=proxies,
    )

    response.raise_for_status()
    return response.text


def clean_html(html):
    text = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ")
    text = re.sub(r"\s+", " ", text)
    return text


def extract_best_price(html, min_realista):
    text = clean_html(html)

    candidates = []

    patterns = [
        # Caso principal do TudoCelular:
        # "O melhor preço para o Motorola Edge 60 Pro no Brasil é de R$ ..."
        r"melhor preço[^R$]{0,180}R\$\s*([\d.]+,\d{2})",

        # Blocos de loja/oferta
        r"(?:oferta|preço|comprar|loja|à vista|pix|boleto)[^R$]{0,160}R\$\s*([\d.]+,\d{2})",

        # Fallback: qualquer preço em R$
        r"R\$\s*([\d.]+,\d{2})",
    ]

    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.I):
            raw = match.group(1)
            price = brl_to_float(raw)

            start = max(0, match.start() - 180)
            end = min(len(text), match.end() + 220)
            context = text[start:end].strip()

            context_lower = context.lower()

            # Evita pegar parcela tipo "10x de R$ 259,90"
            bad_context = [
                "x de r$",
                "vezes de r$",
                "parcelas de r$",
                "parcela de r$",
                "em até",
                "sem juros",
            ]

            if any(term in context_lower for term in bad_context):
                continue

            if price < min_realista:
                continue

            candidates.append({
                "price": price,
                "context": context,
            })

    if not candidates:
        return None

    # Prioriza menor preço realista encontrado
    return sorted(candidates, key=lambda x: x["price"])[0]


def send_email(subject, body):
    smtp_user = os.environ["SMTP_USER"]
    smtp_pass = os.environ["SMTP_PASS"]
    to_email = os.environ["TO_EMAIL"]

    msg = EmailMessage()
    msg["From"] = smtp_user
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(body)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(smtp_user, smtp_pass)
        smtp.send_message(msg)


def main():
    state = load_state()
    changed = False
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    with ALERTS_FILE.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)

        for row in reader:
            ativo = row["ativo"].strip().upper() == "TRUE"
            if not ativo:
                continue

            produto = row["produto"].strip()
            url = row["url"].strip()
            alvo = float(row["alvo"])
            min_realista = float(row.get("min_realista") or 800)

            key = produto.lower().strip()

            print(f"Checking: {produto}")

            try:
                html = fetch_html(url)
                result = extract_best_price(html, min_realista)

                if not result:
                    print(f"  No price found for {produto}")
                    continue

                price = result["price"]
                context = result["context"]

                print(f"  Best found: R$ {price:.2f}")

                previous = state.get(key, {})
                last_alert_price = previous.get("last_alert_price")

                state[key] = {
                    "produto": produto,
                    "url": url,
                    "last_seen_price": price,
                    "last_checked": now,
                    "last_alert_price": last_alert_price,
                    "context": context,
                }

                should_alert = price <= alvo and (
                    last_alert_price is None or price < float(last_alert_price)
                )

                if should_alert:
                    subject = f"Alerta de preço: {produto} por R$ {price:.2f}"
                    body = (
                        f"O preço alvo foi atingido.\n\n"
                        f"Produto: {produto}\n"
                        f"Preço encontrado: R$ {price:.2f}\n"
                        f"Preço alvo: R$ {alvo:.2f}\n\n"
                        f"Página:\n{url}\n\n"
                        f"Trecho encontrado:\n{context}\n"
                    )

                    send_email(subject, body)

                    state[key]["last_alert_price"] = price
                    changed = True

            except Exception as e:
                print(f"  Error checking {produto}: {e}")

    save_state(state)

    if changed:
        print("State changed after alert.")
    else:
        print("No new alerts.")


if __name__ == "__main__":
    main()
