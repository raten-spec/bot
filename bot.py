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
    GitHub'da Settings > Secrets and variables > Actions > VARIABLES
    sekmesinde DRY_RUN=false yapmanız gerekir (Secrets değil).
  - Bot yalnızca ALLOWLIST içindeki sembollerle işlem yapar. Başka
    hiçbir token'a asla emir göndermez.
  - Alış tarafında, TÜM semboller toplamda SWAP.HIVE bakiyenizin
    MAX_ALLOCATION_PCT'sinden fazlasını kullanmaz (bütçe semboller
    arasında eşit bölünür). Satış tarafında her sembol kendi
    bakiyesinin MAX_ALLOCATION_PCT'sini satar.
"""

import os
import sys
import math
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("hive-mm-bot")

# ---------------------------------------------------------------------------
# Yapılandırma — tamamı ortam değişkeni / GitHub Secrets & Variables üzerinden ayarlanır
# ---------------------------------------------------------------------------

HIVE_ACCOUNT = os.environ.get("HIVE_ACCOUNT", "").strip()
HIVE_ACTIVE_KEY = os.environ.get("HIVE_ACTIVE_KEY", "").strip()
DRY_RUN = os.environ.get("DRY_RUN", "true").strip().lower() != "false"
SPREAD_PCT = float(os.environ.get("SPREAD_PCT", "2.0"))          # her yönde %
ORDER_SIZE_QUOTE = float(os.environ.get("ORDER_SIZE_QUOTE", "1"))  # SWAP.HIVE cinsinden, sembol başına üst sınır
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


def floor_to(value, precision):
    """Değeri aşağı yuvarlar (yukarı yuvarlayıp bütçeyi aşmamak için)."""
    factor = 10 ** precision
    return math.floor(value * factor + 1e-9) / factor


def load_clients():
    """nectar/nectarengine bağlantısını kurar. Import burada yapılır ki
    eksik bağımlılıkta hata mesajı net olsun.

    bundle=True: bu Hive örneği üzerinden yapılan işlemler (cancel/buy/sell)
    hemen zincire gönderilmez, kuyruğa eklenir. Tüm semboller işlendikten
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
    """Sembol/SWAP.HIVE emir defterinden en iyi alış ve satışın ortasını döndürür.

    api.find_one() 'limit' parametresi almaz ve tek bir dict (ya da None)
    döner — liste değil. En iyi fiyatı garanti almak için (sıralama
    belirtilmeden find_one hangi kaydı döndüreceğini garanti etmez)
    api.find() 'i limit=1 ve doğru sıralama indexi ile kullanıyoruz:
    buyBook için en yüksek fiyat, sellBook için en düşük fiyat en iyisidir.
    """
    buy_orders = api.find(
        "market", "buyBook", query={"symbol": symbol},
        limit=1, indexes=[{"index": "price", "descending": True}],
    )
    sell_orders = api.find(
        "market", "sellBook", query={"symbol": symbol},
        limit=1, indexes=[{"index": "price", "descending": False}],
    )

    if not buy_orders or not sell_orders:
        return None

    best_bid = float(buy_orders[0]["price"])
    best_ask = float(sell_orders[0]["price"])
    return (best_bid + best_ask) / 2.0


def get_precision(api, symbol):
    info = api.find_one("tokens", "tokens", query={"symbol": symbol})
    if not info:
        return 8
    return int(info["precision"])


def cancel_open_orders(market, wallet, symbol):
    """Botun bu sembol için açık tüm alış/satış emirlerini iptal eder."""
    for order_type, book_fn in (("buy", market.get_buy_book), ("sell", market.get_sell_book)):
        try:
            orders = book_fn(symbol, account=HIVE_ACCOUNT)
        except Exception as e:
            log.warning("%s emir defteri okunamadı (%s): %s", order_type, symbol, e)
            continue

        log.info("%s için %d açık %s emri bulundu.", symbol, len(orders or []), order_type)

        for o in orders or []:
            # Hive Engine'in cancel işlemi emrin txId'sini bekler. _id yedek olarak kalıyor.
            oid = o.get("txId") or o.get("_id")
            if not oid:
                log.warning("%s %s emrinde id bulunamadı, atlandı: %r", symbol, order_type, o)
                continue
            if DRY_RUN:
                log.info("[DRY_RUN] %s %s emri iptal edilirdi (id=%s)", symbol, order_type, oid)
                continue
            try:
                market.cancel(HIVE_ACCOUNT, order_type, oid)
                log.info("%s %s iptali kuyruğa eklendi (id=%s)", symbol, order_type, oid)
            except Exception as e:
                log.warning("%s %s emri kuyruğa eklenemedi (id=%s): %r", symbol, order_type, oid, e)


def get_balance(wallet, symbol):
    try:
        bal = wallet.get_token(symbol)
        if not bal:
            return 0.0
        return float(bal.get("balance", 0))
    except Exception as e:
        log.warning("%s bakiyesi okunamadı: %s", symbol, e)
        return 0.0


def place_quotes(market, wallet, symbol, mid_price, precision, quote_budget):
    """quote_budget: bu sembolün alış emri için harcayabileceği azami SWAP.HIVE."""
    buy_price = round(mid_price * (1 - SPREAD_PCT / 100.0), 8)
    sell_price = round(mid_price * (1 + SPREAD_PCT / 100.0), 8)

    # Alış emri: en fazla min(ORDER_SIZE_QUOTE, quote_budget) SWAP.HIVE harcanır
    spend = min(ORDER_SIZE_QUOTE, quote_budget)
    buy_amount = floor_to(spend / buy_price, precision) if buy_price > 0 else 0

    # Satış emri: elinizdeki `symbol` bakiyesinin MAX_ALLOCATION_PCT'si satılır
    base_balance = get_balance(wallet, symbol)
    sell_amount = floor_to(base_balance * (MAX_ALLOCATION_PCT / 100.0), precision)

    if buy_amount > 0:
        if DRY_RUN:
            log.info("[DRY_RUN] ALIŞ  %s %s @ %s SWAP.HIVE (harcanacak ~%.4f)", buy_amount, symbol, buy_price, spend)
        else:
            try:
                market.buy(HIVE_ACCOUNT, buy_amount, symbol, buy_price)
                log.info("ALIŞ kuyruğa eklendi: %s %s @ %s (~%.4f SWAP.HIVE)", buy_amount, symbol, buy_price, spend)
            except Exception as e:
                log.error("Alış emri kuyruğa eklenemedi (%s): %r", symbol, e)
    else:
        log.info("%s için alış emri atlandı (bütçe/precision nedeniyle miktar 0).", symbol)

    if sell_amount > 0:
        if DRY_RUN:
            log.info("[DRY_RUN] SATIŞ %s %s @ %s SWAP.HIVE", sell_amount, symbol, sell_price)
        else:
            try:
                market.sell(HIVE_ACCOUNT, sell_amount, symbol, sell_price)
                log.info("SATIŞ kuyruğa eklendi: %s %s @ %s", sell_amount, symbol, sell_price)
            except Exception as e:
                log.error("Satış emri kuyruğa eklenemedi (%s): %r", symbol, e)
    else:
        log.info("%s için satış emri atlandı (yetersiz bakiye).", symbol)


def pending_op_count(hive):
    """Kuyruktaki işlem sayısını okumaya çalışır; okunamazsa None döner (tanı amaçlı)."""
    try:
        ops = getattr(getattr(hive, "txbuffer", None), "ops", None)
        return None if ops is None else len(ops)
    except Exception:
        return None


def run():
    if not HIVE_ACCOUNT:
        fail("HIVE_ACCOUNT ortam değişkeni / secret ayarlanmamış.")
    if not DRY_RUN and not HIVE_ACTIVE_KEY:
        fail("DRY_RUN=false iken HIVE_ACTIVE_KEY zorunludur.")

    log.info("Başlıyor. Hesap=%s DRY_RUN=%s SPREAD=%%%s ORDER_SIZE=%s SWAP.HIVE MAX_ALLOC=%%%s",
             HIVE_ACCOUNT, DRY_RUN, SPREAD_PCT, ORDER_SIZE_QUOTE, MAX_ALLOCATION_PCT)
    log.info("Allowlist: %s", ", ".join(ALLOWLIST_BASE_SYMBOLS))
    if not DRY_RUN:
        log.info("Bundle modu aktif: tüm semboller için işlemler tek Hive transaction'ında gönderilecek.")

    hive, api, market, wallet = load_clients()
    queued_any = False
    failed = False

    # Toplam alış bütçesi: SWAP.HIVE bakiyesinin MAX_ALLOCATION_PCT'si,
    # semboller arasında eşit bölünür. Böylece tüm semboller birlikte bile
    # bu sınırı aşmaz.
    quote_balance = get_balance(wallet, QUOTE_SYMBOL)
    total_budget = quote_balance * (MAX_ALLOCATION_PCT / 100.0)
    per_symbol_budget = total_budget / max(len(ALLOWLIST_BASE_SYMBOLS), 1)
    log.info("%s bakiyesi: %.4f | toplam alış bütçesi: %.4f | sembol başına: %.4f",
             QUOTE_SYMBOL, quote_balance, total_budget, per_symbol_budget)

    for symbol in ALLOWLIST_BASE_SYMBOLS:
        log.info("--- %s/%s işleniyor ---", symbol, QUOTE_SYMBOL)
        try:
            mid = get_mid_price(api, symbol)
        except Exception as e:
            log.error("%s için fiyat okunamadı: %r", symbol, e)
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
        place_quotes(market, wallet, symbol, mid, precision, per_symbol_budget)
        queued_any = True

    if DRY_RUN:
        log.info("[DRY_RUN] Hiçbir şey zincire gönderilmedi.")
    elif queued_any:
        count = pending_op_count(hive)
        if count is not None:
            log.info("Gönderim öncesi kuyrukta %d işlem var.", count)
            if count == 0:
                log.warning("Kuyruk boş görünüyor: market işlemleri bu Hive örneğinin "
                            "kuyruğuna girmemiş olabilir. Yine de gönderim deneniyor.")
        log.info("Tüm semboller kuyruğa eklendi, tek transaction olarak gönderiliyor…")
        try:
            result = hive.broadcast()
            trx_id = result.get("trx_id") if isinstance(result, dict) else None
            log.info("Transaction gönderildi. trx_id=%s", trx_id)
            log.info("Not: Hive Engine (sidechain) sonucu ayrıca doğrulanmalı; "
                     "trx_id başarılı Hive girişi demektir, emirlerin kabulü değil.")
        except Exception as e:
            # log.exception tam traceback yazar; boş mesajlı istisnalarda da sebep görünür.
            log.exception("Toplu transaction gönderilemedi: %r", e)
            failed = True
    else:
        log.info("Kuyruğa eklenecek bir şey olmadı, transaction gönderilmedi.")

    log.info("Tamamlandı.")
    if failed:
        # Workflow'un kırmızı görünmesi için: sessiz başarısızlık olmasın.
        sys.exit(1)


if __name__ == "__main__":
    run()
