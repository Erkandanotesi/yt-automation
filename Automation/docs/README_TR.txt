TECH NEWS OTOMASYONU — KISA KULLANIM KILAVUZU
=============================================

1. KLASÖR DÜZENİ
----------------

Automation kökünde yalnızca sabit giriş dosyaları ve kurulum dosyaları bulunur:

- create_video.bat: etkileşimli tek düğmeli üretim
- news-studio.bat: komut satırı arayüzü
- setup.bat: kurulum
- .env: gizli anahtarlar; Git tarafından yok sayılır
- requirements.txt

Kaynak kod `src`, RSS/profil ayarları `config`, çalışma verileri `data`, belgeler
`docs`, düzenleme referansları `references`, yardımcı başlatıcılar `scripts`
klasöründedir. Bütün yolların tek kaynağı `src\file_locations.py` dosyasıdır.

`data\processed_urls.json` ve medya indeksleri üretim durumudur; Git'e eklenmez.
`data\news_archive` çekilen haber metinlerini ID + SHA-256 ile saklar. Aynı metin
yerel olarak engellenir; farklı feed'deki muhtemel aynı olaylar yalnızca gerektiğinde
AI ile karşılaştırılır ve sonuç hash'lerle önbelleğe alınır.
Çekilen sayfa ayrıca hata/CAPTCHA/erişim engeli, kısa veya tekrarlanan metin,
başlık uyumsuzluğu ve düşük makale kalitesi kontrollerinden geçer.
`-orijinal.txt` dosyaları okunabilir makale paragraflarıyla kaydedilir.
Bozuk bir kaynak reddedilince aynı sıralama isteğinde hazırlanan RSS yedeklerinden
sıradaki haber denenir. Yeterli sağlam haber yoksa eksik proje durur; bozuk metin kullanılmaz.

2. KURULUM
----------

setup.bat

Ardından `.env` içindeki API anahtarlarını ve ses kimliğini kontrol et. RSS dosyaları:

config\feeds.opml
config\gaming_feeds.opml

3. ÜCRETSİZ KONTROL
-------------------

news-studio.bat new --category tech --dry-run

Bu komut dosya yazmaz ve hiçbir API isteği yapmaz. Planlanan aşamalarla olası istek
sayılarını gösterir.

Tam yerel test:

scripts\test_program.bat

`.env` değerlerini göstermeden kodlama, tekrar ve bozuk Türkçe yorum kontrolü:

scripts\env_doctor.bat status
scripts\env_doctor.bat fix

4. TEK DÜĞMELİ ÜRETİM
---------------------

create_video.bat

veya:

news-studio.bat new --category tech --confirm-api-costs

İlk çalışma haberleri, orijinal metinleri, sesleri ve zamanlamayı üretir; ardından
`Video N\03_manual_separators` klasörünü oluşturup bekler.
Bu anda önce okunacak `Haberler`, ardından separator dosyalarının kaydedileceği
`03_manual_separators` klasörü otomatik açılır.

Manuel separator dosyalarını ekledikten sonra:

news-studio.bat resume "Video N" --confirm-api-costs

Akış storyboard, haber başına iki AI görseli, arşiv görselleri, altyazı, efektler,
TV katmanları, glitch/SFX ve tekrarlanan/duck edilmiş müziği hazırlar. Sonra 60
saniyelik kontrol videosunda durur.

Kontrol videosunu onaylamak ve devam etmek:

news-studio.bat resume "Video N" --confirm-api-costs --approve-preview

Onay kontrol videosunun SHA-256 değerine bağlıdır. Dosya değişirse onay otomatik
geçersiz olur. Onaydan sonra tam render ve Milestone 9 yayın paketi tamamlanır.

5. DURUM VE KURTARMA
--------------------

news-studio.bat status "Video N"
news-studio.bat validate "Video N"
news-studio.bat resume "Video N" --confirm-api-costs

Her aşama önbellek ve manifest kontrolü yapar. Tamamlanan geçerli aşamalar yeniden
çalıştırılmaz. Ctrl+C mevcut çıktıları korur. Aynı projede iki işlem çalışmasını
`.news_studio.lock` engeller. İşlem gerçekten kapanmışsa:

news-studio.bat resume "Video N" --confirm-api-costs --recover-stale-lock

Olay günlüğü:

Video N\orchestration\events.jsonl

Aşama logları:

Video N\orchestration\logs\

6. TEK AŞAMA KOMUTLARI
----------------------

news-studio.bat run-stage "Video N" captions
news-studio.bat run-stage "Video N" effects
news-studio.bat preview "Video N"
news-studio.bat approve-preview "Video N"
news-studio.bat render "Video N"

Eski ayrıntılı başlatıcılar `scripts` klasöründe tutulur.

7. MEDYA İNDEKSİ
----------------

scripts\media_index.bat

Kod karşılığı:

.\.venv\Scripts\python.exe src\media_index.py --scan

İndeks konumu:

data\indexes\media_index.json

Hem `Assets\Media` hem `Assets\Media (No-Copyright)` kullanılır. Yerel adaylar
ilgisizse konu bazlı fotoğraf havuzları için Unsplash devreye girer. Uygun sonuç yoksa
üretim durmaz; yerel ve benzersiz bir nötr görsel kullanılır.

8. YAYIN PAKETİ
---------------

Otomatik akış bir düşük reasoning yayın metni isteği ve bir orta kaliteli thumbnail
isteği planlar. Her ikisi de önbelleklidir. Thumbnail, sabit seri düzenini kullanır:
başlık solda %46, tek güçlü haber tasviri sağda %54, siyah/yüksek kontrast temel,
beyaz yazı ve tek kırmızı veya sarı vurgu.

Çıktılar:

Video N\09_publish\title.txt
Video N\09_publish\description.txt
Video N\09_publish\chapters.txt
Video N\09_publish\thumbnail_final.png
Video N\09_publish\youtube_upload_manifest.json

YouTube bölümleri doğrudan description.txt içindedir ve 0:00 ile başlar.

9. YOUTUBE YÜKLEME
------------------

Ücretsiz doğrulama:

.\.venv\Scripts\python.exe src\youtube_upload.py --video N --action validate

Gerçek yükleme ayrı ve açık onaylıdır:

.\.venv\Scripts\python.exe src\youtube_upload.py --video N --action upload --confirm-upload

İlk seferde OAuth Desktop client JSON dosyasını şuraya koy:

.youtube_credentials\client_secret.json

Bu klasör Git tarafından yok sayılır. Varsayılan yayın durumu private'dır; aynı video
hash'inin yanlışlıkla iki kez yüklenmesi engellenir.

10. GÜVENLİK
------------

- `.env`, OAuth tokenları ve API anahtarları Git'e eklenmez.
- API maliyetli akışlar `--confirm-api-costs` olmadan başlamaz.
- Thumbnail üretimi ayrıca kendi maliyet onayını kontrol eder.
- `--dry-run` sıfır yazma ve sıfır API isteği garantisi verir.
- Proje klasörleri yalnızca Tech News altında tam `Video N` adıyla oluşturulur.
