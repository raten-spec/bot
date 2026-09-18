#!/usr/bin/env python3
"""
Hive Engine basit piyasa yapıcı (market maker) botu.

Her çalıştırmada:
  1. İzin verilen (allowlist) her çift için mevcut orta fiyatı okur.
  2. Botun o çiftteki AÇIK emirlerini iptal eder.
  3. Orta fiyatın SPREAD_PCT kadar altına bir alış, üstüne bir satış emri koyar.

Güvenlik notları (LÜTFEN OKUYUN):
  - DRY_RUN=true iken hiçbir gerçek emir gönderilmez, sadece ne
    yapılacağı loglanır. Varsayılan budur. Gerçek emir göndermek için
    GitHub Secrets'ta DRY_RUN=false yapmanız gerekir.
  - Bot yalnızca ALLOWLIST içindeki sembollerle işlem yapar. Başka
    hiçbir token'a asla emir göndermez.
  - Bakiyenizin MAX_ALLOCATION_PCT'ten fazlasını tek seferde
    kullanmaz.
"""

import os
import sys
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("hive-mm-bot")

# ---------------------------------------------------------------------------
# Yapılandırma — tamamı ortam değişkeni / GitHub Secrets üzerinden ayarlanır
# ---------------------------------------------------------------------------

HIVE_ACCOUNT = os.environ.get("HIVE_ACCOUNT", "").strip()
HIVE_ACTIVE_KEY = os.environ.get("HIVE_ACTIVE_KEY", "").strip()
DRY_RUN = os.environ.get("DRY_RUN", "true").strip().lower() != "false"
SPREAD_PCT = float(os.environ.get("SPREAD_PCT", "2.0"))          # her yönde %
ORDER_SIZE_QUOTE = float(os.environ.get("ORDER_SIZE_QUOTE", "1"))  # SWAP.HIVE cinsinden, işlem başına
MAX_ALLOCATION_PCT = float(os.environ.get("MAX_ALLOCATION_PCT", "20"))  # bakiyenin en fazla %'si
QUOTE_SYMBOL = "SWAP.HIVE"

# Yalnızca bu semboller arasında işlem yapılır. Kod hiçbir koşulda bu
# listenin dışına çıkmaz — otomatik keşif YOKTUR.
ALLOWLIST_BASE_SYMBOLS = ["DEC", "SPS", "SWAP.BLURT"]

HIVE_NODES = [
    "https://api.hive.blog",
    "https://anyx.io",
    "https://api.deathwing.me",
]


def fail(msg):
    log.error(msg)
    sys.exit(1)


def load_clients():
    """nectar/nectarengine bağlantısını kurar. Import burada yapılır ki
    eksik bağımlılıkta hata mesajı net olsun.

    bundle=True: bu Hive örneği üzerinden yapılan işlemler (cancel/buy/sell)
    hemen zincire gönderilmez, kuyruğa eklenir. Tüm sembolller işlendikten
    sonra tek bir hive.broadcast() çağrısıyla HEPSİ tek Hive transaction'ı
    olarak gönderilir.

    ÖNEMLİ SINIR: Bu paketleme Hive katmanındadır — transaction ya bütünüyle
    zincire girer ya da hiç girmez. Ama Hive Engine (sidechain) tarafı, bu
    tek transaction içindeki işlemleri yine de birbirinden BAĞIMSIZ ve
    SIRAYLA işler. Örn. iptal işlemi tutar ama hemen ardından gelen alış
    emri bakiye yetersizliğinden sidechain'de reddedilebilir; Hive
    transaction'ı buna rağmen "başarılı" sayılır. Yani "ya hepsi ya hiçbiri"
    garantisi sidechain mantığı seviyesinde YOKTUR — sadece tek imza/tek
    broadcast/garantili sıra kazanılır."""
    try:
        from nectar import Hive
        from nectarengine.api import Api
        from nectarengine.market import Market
        from nectarengine.wallet import Wallet
    except ImportError:
        fail("nectar / nectarengine kurulu değil. `pip install -r requirements.txt` çalıştırın.")

    keys = [] if DRY_RUN else [HIVE_ACTIVE_KEY]
    hive = Hive(node=HIVE_NODES, keys=keys, bundle=(not DRY_RUN))
    api = Api()
    market = Market(blockchain_instance=hive)
    wallet = Wallet(HIVE_ACCOUNT, api=api)
    return hive, api, market, wallet


def get_mid_price(api, symbol):
    """Sembol/SWAP.HIVE emir defterinden en iyi alış ve satışın ortasını döndürür."""
    buy_book = api.find_one("market", "buyBook", query={"symbol": symbol}, limit=1)
    sell_book = api.find_one("market", "sellBook", query={"symbol": symbol}, limit=1)

    if not buy_book or not sell_book:
        return None

    best_bid = float(buy_book[0]["price"])
    best_ask = float(sell_book[0]["price"])
    return (best_bid + best_ask) / 2.0


def get_precision(api, symbol):
    info = api.find_one("tokens", "tokens", query={"symbol": symbol}, limit=1)
    if not info:
        return 8
    return int(info[0]["precision"])


def cancel_open_orders(market, wallet, symbol):
    """Botun bu sembol için açık tüm alış/satış emirlerini iptal eder."""
    for order_type, book_fn in (("buy", market.get_buy_book), ("sell", market.get_sell_book)):
        try:
            orders = book_fn(symbol, account=HIVE_ACCOUNT)
        except Exception as e:
            log.warning("%s emir defteri okunamadı (%s): %s", order_type, symbol, e)
            continue
        for o in orders:
            oid = o.get("_id") or o.get("txId")
            if DRY_RUN:
                log.info("[DRY_RUN] %s %s emri iptal edilirdi (id=%s)", symbol, order_type, oid)
                continue
            try:
                market.cancel(HIVE_ACCOUNT, order_type, oid)
                log.info("%s %s iptali kuyruğa eklendi (id=%s)", symbol, order_type, oid)
            except Exception as e:
                log.warning("%s %s emri kuyruğa eklenemedi (id=%s): %s", symbol, order_type, oid, e)


def get_balance(wallet, symbol):
    try:
        bal = wallet.get_token(symbol)
        if not bal:
            return 0.0
        return float(bal.get("balance", 0))
    except Exception as e:
        log.warning("%s bakiyesi okunamadı: %s", symbol, e)
        return 0.0


def place_quotes(market, wallet, symbol, mid_price, precision):
    buy_price = round(mid_price * (1 - SPREAD_PCT / 100.0), 8)
    sell_price = round(mid_price * (1 + SPREAD_PCT / 100.0), 8)

    # Alış emri: ORDER_SIZE_QUOTE kadar SWAP.HIVE harcanır -> ne kadar `symbol` alınacağı hesaplanır
    quote_balance = get_balance(wallet, QUOTE_SYMBOL)
    spend = min(ORDER_SIZE_QUOTE, quote_balance * (MAX_ALLOCATION_PCT / 100.0))
    buy_amount = round(spend / buy_price, precision) if buy_price > 0 else 0

    # Satış emri: elinizdeki `symbol` bakiyesinin bir kısmı satılır
    base_balance = get_balance(wallet, symbol)
    sell_amount = round(min(base_balance * (MAX_ALLOCATION_PCT / 100.0), base_balance), precision)

    if buy_amount > 0:
        if DRY_RUN:
            log.info("[DRY_RUN] ALIŞ  %s %s @ %s SWAP.HIVE (harcanacak ~%.4f)", buy_amount, symbol, buy_price, spend)
        else:
            try:
                market.buy(HIVE_ACCOUNT, buy_amount, symbol, buy_price)
                log.info("ALIŞ kuyruğa eklendi: %s %s @ %s", buy_amount, symbol, buy_price)
            except Exception as e:
                log.error("Alış emri kuyruğa eklenemedi (%s): %s", symbol, e)
    else:
        log.info("%s için alış emri atlandı (yetersiz SWAP.HIVE bakiyesi ya da limit).", symbol)

    if sell_amount > 0:
        if DRY_RUN:
            log.info("[DRY_RUN] SATIŞ %s %s @ %s SWAP.HIVE", sell_amount, symbol, sell_price)
        else:
            try:
                market.sell(HIVE_ACCOUNT, sell_amount, symbol, sell_price)
                log.info("SATIŞ kuyruğa eklendi: %s %s @ %s", sell_amount, symbol, sell_price)
            except Exception as e:
                log.error("Satış emri kuyruğa eklenemedi (%s): %s", symbol, e)
    else:
        log.info("%s için satış emri atlandı (yetersiz bakiye).", symbol)


def run():
    if not HIVE_ACCOUNT:
        fail("HIVE_ACCOUNT ortam değişkeni / secret ayarlanmamış.")
    if not DRY_RUN and not HIVE_ACTIVE_KEY:
        fail("DRY_RUN=false iken HIVE_ACTIVE_KEY zorunludur.")

    log.info("Başlıyor. Hesap=%s DRY_RUN=%s SPREAD=%%%s ORDER_SIZE=%s SWAP.HIVE",
              HIVE_ACCOUNT, DRY_RUN, SPREAD_PCT, ORDER_SIZE_QUOTE)
    log.info("Allowlist: %s", ", ".join(ALLOWLIST_BASE_SYMBOLS))
    if not DRY_RUN:
        log.info("Bundle modu aktif: tüm semboller için işlemler tek Hive transaction'ında gönderilecek.")

    hive, api, market, wallet = load_clients()
    queued_any = False

    for symbol in ALLOWLIST_BASE_SYMBOLS:
        log.info("--- %s/%s işleniyor ---", symbol, QUOTE_SYMBOL)
        try:
            mid = get_mid_price(api, symbol)
        except Exception as e:
            log.error("%s için fiyat okunamadı: %s", symbol, e)
            continue

        if mid is None or mid <= 0:
            log.warning("%s için emir defteri boş/eksik, bu tur atlanıyor.", symbol)
            continue

        precision = get_precision(api, symbol)
        log.info("%s orta fiyat: %s SWAP.HIVE (precision=%s)", symbol, mid, precision)

        # Not: bundle modunda bu iki çağrı zincire hemen gitmez, kuyruğa
        # eklenir. Sıra korunur: bu sembol için iptaller, alış/satıştan
        # önce kuyruğa girer.
        cancel_open_orders(market, wallet, symbol)
        place_quotes(market, wallet, symbol, mid, precision)
        queued_any = True

    if DRY_RUN:
        log.info("[DRY_RUN] Hiçbir şey zincire gönderilmedi.")
    elif queued_any:
        log.info("Tüm semboller kuyruğa eklendi, tek transaction olarak gönderiliyor…")
        try:
            result = hive.broadcast()
            trx_id = result.get("trx_id") if isinstance(result, dict) else None
            log.info("Transaction gönderildi. trx_id=%s", trx_id)
        except Exception as e:
            log.error("Toplu transaction gönderilemedi: %s", e)
    else:
        log.info("Kuyruğa eklenecek bir şey olmadı, transaction gönderilmedi.")

    log.info("Tamamlandı.")


if __name__ == "__main__":
    run()
