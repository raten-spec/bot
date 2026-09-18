#!/usr/bin/env python3
"""
Hive Engine basit piyasa yapıcı (market maker) botu.

Her çalıştırmada:
  1. İzin verilen (allowlist) her çift için mevcut orta fiyatı okur.
  2. Botun o çiftteki AÇIK emirlerini iptal eder.
  3. Orta fiyatın SPREAD_PCT kadar altına bir alış, üstüne bir satış emri koyar.
  4. (Canlı modda) gönderilen işlemlerin Hive Engine tarafında KABUL EDİLİP
     EDİLMEDİĞİNİ kontrol eder ve reddedilenleri loglar.

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

Hive Engine ücret kuralı (hivesmartcontracts, 28 Mayıs 2025'ten beri):
  Bir kullanıcı için her Hive bloğunda İLK market işlemi ücretsizdir; aynı
  bloktaki her ek market işlemi 0.001 BEED keser. Hesapta BEED yoksa ek
  işlemler sidechain'de "multiTransaction fee" hatasıyla reddedilir.

SEND_DELAY_SECS (varsayılan: 6):
  Bundle kapalıyken iki işlem arasında en az bu kadar saniye beklenir. Hive
  bloğu 3 saniyedir; 6 saniye her işlemin ayrı bir bloğa düşmesini, dolayısıyla
  ek ücret ödememeyi sağlar.

BUNDLE_MODE (varsayılan: false):
  - false: her işlem (iptal/alış/satış) ayrı bir Hive transaction'ı olarak,
    SEND_DELAY_SECS aralıkla gönderilir. BEED gerekmez.
  - true : tüm işlemler tek Hive transaction'ında (yani tek blokta)
    gönderilir. İlk işlem dışındakiler BEED ücreti keser. Yalnızca hesapta
    yeterli BEED varsa açın.
"""

import os
import sys
import json
import math
import time
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
BUNDLE_MODE = os.environ.get("BUNDLE_MODE", "false").strip().lower() == "true"
SEND_DELAY_SECS = float(os.environ.get("SEND_DELAY_SECS", "6"))  # ardışık işlemler arası minimum bekleme
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

# Bundle kapalıyken gönderilen her işlemin (etiket, trx_id) kaydı; sidechain
# sonucunu sonradan kontrol etmek için tutulur.
SENT_TXS = []
# Göndermeye/kuyruğa eklemeye çalışırken alınan hatalar (çıkış kodu için).
SEND_ERRORS = []

SENT_WORD = "kuyruğa eklendi" if BUNDLE_MODE else "gönderildi"


def fail(msg):
    log.error(msg)
    sys.exit(1)


def floor_to(value, precision):
    """Değeri aşağı yuvarlar (yukarı yuvarlayıp bütçeyi aşmamak için)."""
    factor = 10 ** precision
    return math.floor(value * factor + 1e-9) / factor


_last_send_at = None


def pace():
    """Bir önceki işlemden bu yana SEND_DELAY_SECS geçmediyse bekler. Her işlem
    ayrı bir Hive bloğuna düşsün, aynı blokta ikinci market işlemi için BEED
    ücreti kesilmesin diye her gönderimden hemen önce çağrılır. Bundle
    modunda işlemler zaten tek blokta gider, bu yüzden bekleme yapılmaz."""
    global _last_send_at
    if BUNDLE_MODE:
        return
    if _last_send_at is not None:
        wait = SEND_DELAY_SECS - (time.monotonic() - _last_send_at)
        if wait > 0:
            time.sleep(wait)
    _last_send_at = time.monotonic()


def record_tx(label, tx):
    """Bundle kapalıyken gönderilen işlemin trx_id'sini sonradan kontrol için saklar."""
    if BUNDLE_MODE:
        return None
    trx_id = tx.get("trx_id") if isinstance(tx, dict) else None
    if trx_id:
        SENT_TXS.append((label, trx_id))
    else:
        log.warning("%s için trx_id alınamadı, sidechain sonucu kontrol edilemeyecek.", label)
    return trx_id


def load_clients():
    """nectar/nectarengine bağlantısını kurar. Import burada yapılır ki
    eksik bağımlılıkta hata mesajı net olsun.

    BUNDLE_MODE=false (varsayılan): her işlem ayrı Hive transaction'ı olarak
    hemen gönderilir.

    BUNDLE_MODE=true: bundle=True ile işlemler kuyruğa eklenir ve sonunda tek
    hive.broadcast() ile gönderilir. Hive katmanında ya hep ya hiç geçerlidir,
    ama Hive Engine bunu multiTransaction sayar: ilk işlemden sonrakiler için
    BEED ücreti alınır ve sidechain işlemleri birbirinden BAĞIMSIZ işler.
    BEED yoksa geri kalan işlemler reddedilir, Hive transaction'ı yine de
    'başarılı' görünür."""
    try:
        from nectar import Hive
        from nectarengine.api import Api
        from nectarengine.market import Market
        from nectarengine.wallet import Wallet
    except ImportError:
        fail("nectar / nectarengine kurulu değil. `pip install -r requirements.txt` çalıştırın.")

    keys = [] if DRY_RUN else [HIVE_ACTIVE_KEY]
    hive = Hive(node=HIVE_NODES, keys=keys, bundle=(BUNDLE_MODE and not DRY_RUN))
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
            pace()
            try:
                tx = market.cancel(HIVE_ACCOUNT, order_type, oid)
                log.info("%s %s iptali %s (id=%s)", symbol, order_type, SENT_WORD, oid)
                record_tx("%s %s iptali" % (symbol, order_type), tx)
            except Exception as e:
                log.warning("%s %s iptali gönderilemedi (id=%s): %r", symbol, order_type, oid, e)
                SEND_ERRORS.append("%s %s iptali" % (symbol, order_type))


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
            pace()
            try:
                tx = market.buy(HIVE_ACCOUNT, buy_amount, symbol, buy_price)
                log.info("ALIŞ %s: %s %s @ %s (~%.4f SWAP.HIVE)", SENT_WORD, buy_amount, symbol, buy_price, spend)
                record_tx("%s alış" % symbol, tx)
            except Exception as e:
                log.error("Alış emri gönderilemedi (%s): %r", symbol, e)
                SEND_ERRORS.append("%s alış" % symbol)
    else:
        log.info("%s için alış emri atlandı (bütçe/precision nedeniyle miktar 0).", symbol)

    if sell_amount > 0:
        if DRY_RUN:
            log.info("[DRY_RUN] SATIŞ %s %s @ %s SWAP.HIVE", sell_amount, symbol, sell_price)
        else:
            pace()
            try:
                tx = market.sell(HIVE_ACCOUNT, sell_amount, symbol, sell_price)
                log.info("SATIŞ %s: %s %s @ %s", SENT_WORD, sell_amount, symbol, sell_price)
                record_tx("%s satış" % symbol, tx)
            except Exception as e:
                log.error("Satış emri gönderilemedi (%s): %r", symbol, e)
                SEND_ERRORS.append("%s satış" % symbol)
    else:
        log.info("%s için satış emri atlandı (yetersiz bakiye).", symbol)


def verify_signing_key(hive):
    """Verilen private key'in, hesabın zincirdeki active (ya da owner) public
    key'lerinden biriyle eşleşip eşleşmediğini kontrol eder. Böylece
    MissingKeyError'a broadcast aşamasında değil, işe başlamadan önce ve
    anlaşılır bir mesajla yakalanır. Private key asla loglanmaz.

    Kontrolün kendisi çalışamazsa (import/API hatası) yalnızca uyarı verir
    ve devam eder; bu durumda gerçek imza hatası broadcast'te görünür."""
    try:
        from nectar.account import Account
        from nectargraphenebase.account import PrivateKey

        # "STM"/"TST" gibi prefix farklarından etkilenmemek için ilk 3 karakter atılır.
        my_pub = str(PrivateKey(HIVE_ACTIVE_KEY).pubkey)[3:]

        acc = Account(HIVE_ACCOUNT, blockchain_instance=hive)
        signing_keys = set()
        for role in ("active", "owner"):
            for entry in acc[role]["key_auths"]:
                signing_keys.add(str(entry[0])[3:])
        posting_keys = {str(e[0])[3:] for e in acc["posting"]["key_auths"]}
        memo_key = str(acc["memo_key"])[3:]
    except Exception as e:
        log.warning("Anahtar ön kontrolü yapılamadı, devam ediliyor: %r", e)
        return

    if my_pub in signing_keys:
        log.info("Anahtar doğrulandı: HIVE_ACTIVE_KEY, %s hesabının active/owner yetkisine ait.", HIVE_ACCOUNT)
        return
    if my_pub in posting_keys:
        fail("HIVE_ACTIVE_KEY olarak bir POSTING key girilmiş. Active private key gerekir.")
    if my_pub == memo_key:
        fail("HIVE_ACTIVE_KEY olarak MEMO key girilmiş. Active private key gerekir.")
    fail("HIVE_ACTIVE_KEY, %s hesabının active/owner key'lerinden hiçbiriyle eşleşmiyor. "
         "Key'i ve HIVE_ACCOUNT adını kontrol edin (başka bir hesabın key'i olabilir)." % HIVE_ACCOUNT)


def pending_op_count(hive):
    """Kuyruktaki işlem sayısını okumaya çalışır; okunamazsa None döner (tanı amaçlı)."""
    try:
        ops = getattr(getattr(hive, "txbuffer", None), "ops", None)
        return None if ops is None else len(ops)
    except Exception:
        return None


def check_sidechain_results(api, tx_ids, wait_rounds=6, wait_secs=5):
    """Gönderilen işlemlerin Hive Engine (sidechain) tarafında kabul edilip
    edilmediğini kontrol eder. Hive transaction'ının başarılı olması, emrin
    kabul edildiği anlamına GELMEZ; reddedilenler burada loglanır.

    tx_ids: [(etiket, sidechain_txid), ...]
    Dönüş: en az bir işlem reddedildiyse True."""
    pending = list(tx_ids)
    rejected = False
    for _ in range(wait_rounds):
        time.sleep(wait_secs)
        still_pending = []
        for label, txid in pending:
            try:
                info = api.get_transaction_info(txid)
            except Exception as e:
                log.warning("%s sonucu sorgulanamadı (%s): %r", label, txid, e)
                still_pending.append((label, txid))
                continue
            if not info:
                still_pending.append((label, txid))  # sidechain henüz işlememiş olabilir
                continue

            logs = info.get("logs") if isinstance(info, dict) else None
            if isinstance(logs, str):
                try:
                    logs = json.loads(logs)
                except ValueError:
                    logs = {"errors": [logs]}
            errors = logs.get("errors") if isinstance(logs, dict) else None
            if errors:
                log.error("Sidechain REDDETTİ: %s (%s): %s", label, txid, "; ".join(str(x) for x in errors))
                rejected = True
            else:
                log.info("Sidechain kabul etti: %s (%s)", label, txid)
        pending = still_pending
        if not pending:
            break

    for label, txid in pending:
        log.warning("Sidechain sonucu henüz görünmedi: %s (%s). Explorer'dan kontrol edin.", label, txid)
    return rejected


def run():
    if not HIVE_ACCOUNT:
        fail("HIVE_ACCOUNT ortam değişkeni / secret ayarlanmamış.")
    if not DRY_RUN and not HIVE_ACTIVE_KEY:
        fail("DRY_RUN=false iken HIVE_ACTIVE_KEY zorunludur.")

    log.info("Başlıyor. Hesap=%s DRY_RUN=%s BUNDLE=%s SPREAD=%%%s ORDER_SIZE=%s SWAP.HIVE MAX_ALLOC=%%%s",
             HIVE_ACCOUNT, DRY_RUN, BUNDLE_MODE, SPREAD_PCT, ORDER_SIZE_QUOTE, MAX_ALLOCATION_PCT)
    log.info("Allowlist: %s", ", ".join(ALLOWLIST_BASE_SYMBOLS))
    if not DRY_RUN:
        if BUNDLE_MODE:
            log.info("Bundle modu aktif: işlemler tek blokta gönderilecek; ilk işlem dışındakiler 0.001 BEED keser.")
        else:
            log.info("Bundle kapalı: her işlem ayrı Hive transaction'ı olarak, en az %.0f sn arayla gönderilecek.", SEND_DELAY_SECS)

    hive, api, market, wallet = load_clients()
    if not DRY_RUN:
        verify_signing_key(hive)
    queued_any = False
    failed = False
    tx_ids = []

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

        # Sıra korunur: bu sembol için iptaller, alış/satıştan önce gönderilir/kuyruğa girer.
        cancel_open_orders(market, wallet, symbol)
        place_quotes(market, wallet, symbol, mid, precision, per_symbol_budget)
        queued_any = True

    if DRY_RUN:
        log.info("[DRY_RUN] Hiçbir şey zincire gönderilmedi.")
    elif BUNDLE_MODE:
        if queued_any:
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
                if trx_id:
                    # Hive Engine, çoklu işlemleri trx_id, trx_id-1, trx_id-2 ... olarak ayırır.
                    n = count if count else 1
                    tx_ids = [("işlem #%d" % (i + 1), trx_id if i == 0 else "%s-%d" % (trx_id, i))
                              for i in range(n)]
            except Exception as e:
                # log.exception tam traceback yazar; boş mesajlı istisnalarda da sebep görünür.
                log.exception("Toplu transaction gönderilemedi: %r", e)
                failed = True
        else:
            log.info("Kuyruğa eklenecek bir şey olmadı, transaction gönderilmedi.")
    else:
        tx_ids = list(SENT_TXS)
        log.info("Ayrı ayrı gönderilen işlem sayısı: %d", len(tx_ids))

    if tx_ids:
        log.info("Hive Engine tarafındaki sonuç kontrol ediliyor…")
        if check_sidechain_results(api, tx_ids):
            failed = True

    if SEND_ERRORS:
        log.error("Gönderilemeyen işlemler: %s", ", ".join(SEND_ERRORS))
        failed = True

    log.info("Tamamlandı.")
    if failed:
        # Workflow'un kırmızı görünmesi için: sessiz başarısızlık olmasın.
        sys.exit(1)


if __name__ == "__main__":
    run()
