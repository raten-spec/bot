# Hive Engine Piyasa Yapıcı Botu

Basit bir "piyasa yapıcı" (market maker) botu. DEC, SPS ve SWAP.BLURT
için SWAP.HIVE karşılığında orta fiyatın etrafına alış/satış emirleri
koyar, her çalıştırmada eski emirlerini iptal edip yenisini açar.

## ÖNCE OKUYUN — Güvenlik

- Bu repo **public** ise, `HIVE_ACTIVE_KEY`'i asla kod içine yazmayın,
  yalnızca GitHub **Secrets**'a ekleyin (aşağıda anlatılıyor).
  Secrets, workflow log'larında otomatik olarak `***` ile maskelenir.
- Varsayılan olarak bot **DRY_RUN=true** ile çalışır: hiçbir gerçek
  emir gönderilmez, sadece ne yapacağını loglar. Birkaç çalışmasını
  loglardan izleyip mantıklı geldiğine emin olmadan `DRY_RUN=false`
  yapmayın.
- Mevcut ana hesabınızı kullanıyorsunuz — active key hesabınızın
  tamamına (sadece bu token'lara değil) yetkilidir. Bir kod hatası
  riski tamamen sizin kararınız dahilinde kabul edilmiştir.

## Kurulum

1. Bu klasörü kendi GitHub reponuza yükleyin (public ya da private,
   fark etmez — Secrets her ikisinde de şifreli tutulur).

2. Repo → **Settings → Secrets and variables → Actions → Secrets**
   kısmına şunları ekleyin:
   - `HIVE_ACCOUNT` — Hive kullanıcı adınız (örn. `myaccount`)
   - `HIVE_ACTIVE_KEY` — active private key'iniz (5xxxx... ile başlar)

3. Aynı sayfada **Variables** sekmesine (isteğe bağlı, hepsi
   varsayılan değerlere sahip) şunları ekleyebilirsiniz:
   - `DRY_RUN` — `true` (varsayılan, güvenli) ya da `false`
   - `SPREAD_PCT` — her yönde yüzde kaç spread (varsayılan `2.0`)
   - `ORDER_SIZE_QUOTE` — alış emri başına harcanacak SWAP.HIVE miktarı
     (varsayılan `1`)
   - `MAX_ALLOCATION_PCT` — bakiyenizin tek seferde en fazla yüzde
     kaçının kullanılacağı (varsayılan `20`)

4. **Actions** sekmesinden workflow'u elle bir kere çalıştırın
   (`workflow_dispatch`) ve logları okuyun. `DRY_RUN=true` iken
   hiçbir risk yoktur, istediğiniz kadar deneyin.

5. Loglar mantıklı görünüyorsa, Variables'a `DRY_RUN=false` ekleyin.
   Bot artık her ~5 dakikada bir gerçek emir gönderecek.

## Tek transaction (bundle) modu

Bot, DRY_RUN=false iken bir çalıştırmadaki TÜM işlemleri (tüm sembollerin
iptalleri + alış + satış emirleri) **tek bir Hive transaction'ında**
toplayıp tek seferde gönderir (`nectar`'ın `bundle=True` modu). Bu size
tek imza, tek RPC round-trip ve garantili işlem sırası kazandırır.

**Bilerek kabul edilmiş sınır:** Bu paketleme Hive katmanındadır. Hive
Engine (sidechain) tarafı, bu tek transaction içindeki işlemleri yine de
birbirinden bağımsız ve sırayla işler — örneğin bir iptal işlemi tutar
ama hemen ardından gelen alış emri bakiye yetersizliğinden sidechain
tarafından reddedilebilir; Hive transaction'ı buna rağmen zincire girmiş
sayılır. Yani "ya hepsi ya hiçbiri" garantisi sidechain mantığı
seviyesinde yoktur.

## Sınırlamalar (bilerek kabul edilmiş riskler)

- GitHub Actions'ın zamanlanmış (`cron`) görevleri **tam olarak** 5
  dakikada bir garanti çalışmaz; yoğun saatlerde gecikebilir. Bu bot
  bu yüzden geniş bir spread (`SPREAD_PCT`) kullanır — piyasa hızlı
  hareket ederse emirleriniz "bayat" kalabilir.
- Bot yalnızca kod içinde sabit tanımlı `ALLOWLIST_BASE_SYMBOLS`
  listesindeki sembollerle işlem yapar. Yeni bir token eklemek
  isterseniz `bot.py` içindeki listeyi elle güncellemeniz gerekir —
  bot kendi başına yeni token keşfedip eklemez.
- Piyasa yapıcılık kâr garantisi değildir. Fiyat sürekli tek yöne
  hareket ederse (trend), bot o yönde birikip zararla oturabilir
  (envanter riski). `MAX_ALLOCATION_PCT` bunun etkisini sınırlar,
  ortadan kaldırmaz.
