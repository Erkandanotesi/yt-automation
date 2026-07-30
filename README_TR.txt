NEWS STUDIO OTOMASYONU — TÜRKÇE KULLANIM
=========================================

1. KLASÖR DÜZENİ
----------------

Ana çalışma klasörü:

D:\Dosyalar\İş\YouTube\Tech News

Automation yalnızca kod, ayar, bağımlılık ve otomasyon durum verilerini tutar:

Tech News\
├── Assets\
│   ├── Audio\
│   │   └── background_music\
│   ├── Media\
│   ├── Outro\
│   └── Overlays\
├── Automation\
│   ├── .env
│   ├── AUTOMATION.md
│   ├── create_video.bat
│   ├── news_pipeline.py
│   ├── elevenlabs_tts.py
│   ├── preflight.bat
│   ├── processed_urls.json
│   └── ...
├── Media (No-Copyright)\
├── Video 1\
├── Video 2\
├── Video 3\
└── Video 4\

Yeni proje klasörleri Automation içine değil, Tech News ana klasörüne yazılır.
Yalnızca tam olarak "Video N" biçimindeki klasörler numaralandırmaya katılır.
Video 1–Video 4 varsa sıradaki proje Video 5 olur.


2. GİT VE GİZLİ BİLGİLER
------------------------

Git deposu Automation klasöründedir.

.env aynı klasörde kalır fakat .gitignore tarafından dışlanır. API anahtarları
Git'e eklenmez. .env.example yalnızca güvenli örnek değerleri içerir.

Kontrol:

git check-ignore -v .env

Beklenen sonuç .gitignore içindeki .env kuralıdır.


3. KURULUM
----------

setup.bat dosyasına çift tıkla.

Kurulum:

- .venv Python ortamını oluşturur;
- requirements.txt paketlerini kurar;
- eksikse .env dosyasını .env.example üzerinden oluşturur;
- FFmpeg ve FFprobe'u Automation\tools\ffmpeg altına taşınabilir olarak kurar;
- eksik şablon dosyalarını oluşturur.

Sistem genelinde yönetici yetkili FFmpeg kurulumu gerekmez.


4. ÖN KONTROL
-------------

preflight.bat dosyasına çift tıkla veya:

.\.venv\Scripts\python.exe preflight.py

Kontrol edilenler:

- Python sürümü;
- Automation ve proje köklerinin ayrılmış olması;
- sıradaki Video N klasörü;
- yazma izinleri;
- .env dosyasının Git'ten dışlanması;
- seçilen AI ve ElevenLabs ayarlarının varlığı;
- feed ve kanal profil dosyaları;
- FFmpeg ve FFprobe;
- boş disk alanı.

Tüm zorunlu kontroller PASS ve sonuç READY olmalıdır.


5. .ENV AYARLARI
----------------

Klasör değerleri göreli tutulur:

AUTOMATION_ROOT=.
PROJECTS_ROOT=..

Göreli yollar Automation klasörüne göre çözülür.

AI sağlayıcısı:

AI_PROVIDER=openai
OPENAI_API_KEY=GERCEK_ANAHTAR

veya:

AI_PROVIDER=gemini
GEMINI_API_KEY=GERCEK_ANAHTAR

ElevenLabs:

ELEVENLABS_API_KEY_1=GERCEK_ANAHTAR
ELEVENLABS_API_KEY_2=ISTEGE_BAGLI_IKINCI_ANAHTAR
ELEVENLABS_VOICE_ID=SES_KIMLIGI

Gerçek anahtarlar loglara veya Git'e yazılmaz.


6. YENİ VİDEO OLUŞTURMA
-----------------------

create_video.bat dosyasına çift tıkla.

Seçimler:

1  Tech News için yeni proje
2  Gaming için yeni proje
0  En son Video N projesinin yalnızca TTS aşaması

"o" eki mevcut çıktıları yeniden üretmek için kullanılır:

1 o
2 o
0 o

Program önce preflight çalıştırır, sonra Tech News ana klasöründeki en büyük
"Video N" numarasını bulur.


7. KOMUT SATIRI KULLANIMI
-------------------------

Automation klasöründe:

Video 5 için teknoloji haberleri:

.\.venv\Scripts\python.exe news_pipeline.py --video 5 --category tech

Video 5 için oyun haberleri:

.\.venv\Scripts\python.exe news_pipeline.py --video 5 --category gaming

Video 5 seslerini üret:

.\.venv\Scripts\python.exe elevenlabs_tts.py --video 5

Mevcut sesleri yeniden üret:

.\.venv\Scripts\python.exe elevenlabs_tts.py --video 5 --overwrite

ElevenLabs ayarlarını kontrol et:

.\.venv\Scripts\python.exe elevenlabs_tts.py --check

Hesaptaki sesleri listele:

.\.venv\Scripts\python.exe elevenlabs_tts.py --list-voices


8. MEVCUT ÇIKTILAR
------------------

Her "Video N" klasöründe:

Haberler\01_haber-basligi.txt
- ElevenLabs'e gönderilen 150–200 kelimelik anlatım metni.

Yeni dosya adlarında başlıktan bağımsız sabit bir hikâye kimliği de bulunur:

Haberler\01_story_a1b2c3d4e5f6_haber-basligi.txt

Haberler\01_haber-basligi-orijinal.txt
- Kaynaktan çıkarılan orijinal haber metni.

Sesler\01_haber-basligi.mp3
- Anlatım metninden üretilen ses.

aday_haberler.json
- RSS taramasında bulunan adaylar.

video_script.txt
- Seçilen anlatım metinlerinin birleşimi.

project_info.json
- Proje durumu, sabit hikâye kimlikleri, kaynak, sıralama, doğrulama ve dosya
  SHA-256 özetleri.

Sesler\tts_info.json
- Ses ayarları, giriş parmak izi, istek kimliği, karakter maliyeti, deneme geçmişi,
  süre, codec ve ses SHA-256 özetleri.

pipeline.log ve elevenlabs.log
- Teknik işlem günlükleri.


9. MILESTONE 1 GÜVENLİK VE ÖNBELLEK DAVRANIŞI
---------------------------------------------

- Haber sıralaması ana seçimlerle birlikte yedek adaylar da döndürür. Bir sayfa
  okunamazsa ikinci bir sıralama API çağrısı yapılmadan sıradaki yedek denenir.
- REQUIRE_FULL_TOP_N=true iken tam TOP_N geçerli haber oluşmazsa proje
  "incomplete" kalır ve processed_urls.json güncellenmez.
- Her anlatım 150–200 kelime, boş olmayan kaynak, destekli sayı/doğrudan alıntı,
  benzersiz kaynak ve azalan puan sırası için doğrulanır.
- Dosyalar geçici dosya + atomik değiştirme yöntemiyle yazılır. Kesinti eski sağlam
  dosyayı bozmaz.
- TTS yalnızca metin veya ses ayarlarının parmak izi değiştiğinde yeniden üretilir.
  Aynı girişte mevcut MP3'ün SHA-256 özeti ve FFprobe sonucu geçerliyse API çağrısı
  yapılmadan korunur.
- project_info.json "complete" değilse TTS başlamaz. Böylece eksik haber grubu
  yanlışlıkla seslendirilmez.
- ELEVENLABS_API_KEY_1, _2, _3 ... biçimindeki tüm dolu numaralı anahtarlar sabit
  bir üst sınır olmadan ve anahtar değerleri loglanmadan okunur.
- Kota/erişim hatasında sıradaki yetkili anahtar denenebilir; geçersiz metin gibi
  kalıcı istek hatalarında hemen durulur. IP değiştirme veya sağlayıcı denetimini
  gizleme uygulanmaz.

Yerel test (OpenAI/ElevenLabs üretim kotası kullanmaz):

.\.venv\Scripts\python.exe -m unittest discover -s tests -v


10. ALTYAZI ZAMANLAMASI
-----------------------

Whisper zorunlu değildir ve bu proje için planlanan ana yöntem değildir.

Yeni seslerde ElevenLabs "with timestamps" yanıtı kullanılır. Orijinal metne ait
karakter zamanları MP3 ile aynı çalışmada kaydedilir.

Daha önce üretilmiş MP3 dosyaları ElevenLabs Forced Alignment API ile mevcut
onaylı metne hizalanacaktır. Böylece altyazı metni tekrar transkripsiyonla
değişmez.

Normal akışta create_video.bat TTS'den sonra zamanlama aşamasını otomatik çalıştırır.

Elle çalıştırma:

.\.venv\Scripts\python.exe alignment_stage.py --video 5

Birinci haber için timer, transcript ve cümle sınırı kontrol videosu:

.\.venv\Scripts\python.exe alignment_stage.py --video 5 --diagnostic --story 1

Üretilen dosyalar:

Sesler\alignments\<ses-adi>.alignment.json
- Canonical metin, karakter, kelime, phrase ve cümle zamanları; ses/metin SHA-256
  değerleri; final kelime sapması; önemli iddialar, entity adayları ve görsel sorguları.

story_timing.json
- Her haberden önce ayrılmış separator aralığı, global narration başlangıç/bitişi,
  global cümle/phrase/görsel-beat zamanları ve overlap kalite kapısı.

timing_diagnostic_story_01.mp4
- Yalnızca --diagnostic istendiğinde oluşan 1280×720 yerel kontrol videosu.

Önbellek davranışı:

- Geçerli alignment dosyası metin ve MP3 SHA-256 değerleri değişmedikçe korunur.
- Yeni timestamped ses için ikinci hizalama API çağrısı yapılmaz.
- Eski MP3'te alignment yoksa yalnızca Forced Alignment çağrısı yapılır; MP3 yeniden
  üretilmez.
- Sağlayıcının döndürdüğü metin onaylı narration ile karakter karakter aynı değilse
  işlem durur.
- Son kelime alignment bitişinden 100 ms'den fazla farklıysa kalite kapısı geçmez.


11. MANUEL TECH NEWS SEPARATOR CHECKPOINT
-----------------------------------------

Zamanlama bittikten sonra otomasyon önce şu klasörü kendisi oluşturur:

Video N\03_manual_separators\

Beş haber için beklenen adlar:

01_separator.mp4
02_separator.mp4
03_separator.mp4
04_separator.mp4
05_separator.mp4

Ardından otomasyon şu mesajla bekler:

The summary and the original news texts are saved, waiting for the animation
production. Press 1 after ready, or 0 for cancelling all process.

Animasyonları klasöre koyduktan sonra 1:

- her dosyayı FFprobe ile inceler;
- tam FFmpeg decode testi yapar;
- varsayılan olarak 1920×1080, 30 fps, H.264/HEVC ve yuv420p/yuv420p10le kabul eder;
- süreyi, video stream'ini ve ses stream'inin varlığını kaydeder;
- bütün separator dosyalarında ses varlığının tutarlı olmasını ister;
- gerçek separator süreleriyle story_timing.json global zamanlarını yeniden hesaplar.

0 işlemi durdurur fakat haberleri, sesleri, alignment dosyalarını ve separator
animasyonlarını silmez. state.json durumu CANCELLED olarak kaydeder.

Pencereyi kapatırsan durum WAITING kalır. Haber/TTS aşamalarını tekrarlamadan devam:

resume_video.bat

veya:

.\.venv\Scripts\python.exe manual_checkpoint.py --video 5

Komut satırı işlemleri:

.\.venv\Scripts\python.exe manual_checkpoint.py --video 5 --action prepare
.\.venv\Scripts\python.exe manual_checkpoint.py --video 5 --action validate
.\.venv\Scripts\python.exe manual_checkpoint.py --video 5 --action status
.\.venv\Scripts\python.exe manual_checkpoint.py --video 5 --action cancel

Onaylanmış bir separator daha sonra değişir veya silinirse SHA-256 kontrolü READY
durumunu otomatik olarak WAITING'e döndürür.


12. STORYBOARD VE HABER BAŞINA İKİ AI GÖRSELİ
---------------------------------------------

Manuel separator checkpoint READY olduktan sonra:

storyboard_video.bat

komutu her haberin onaylı anlatımını, orijinal kaynak metnini ve alignment zamanlarını
okur. gpt-5.6-luna tam olarak iki farklı görsel an seçer ve API'ye gönderilecek promptu
yazar. Görselin kendisini ayrı gpt-image-2 modeli üretir.

Varsayılan dengeli ayarlar:

STORYBOARD_MODEL=gpt-5.6-luna
OPENAI_IMAGE_MODEL=gpt-image-2
OPENAI_IMAGE_QUALITY=medium
OPENAI_IMAGE_SIZE=1280x720

Ücretli görsel isteği otomatik başlamaz. storyboard_video.bat önce seçim ister:

1  Yalnızca birinci haber için 2 görsel ve 30 saniyelik proof
2  Bütün haberler için 2'şer görsel ve birinci haber proof
0  Yalnızca storyboard.json kaydet; görsel üretme

Elle kullanım:

.\.venv\Scripts\python.exe storyboard_stage.py --video 5 --action plan
.\.venv\Scripts\python.exe storyboard_stage.py --video 5 --action generate --story 1 --confirm-cost --max-requests 2
.\.venv\Scripts\python.exe storyboard_stage.py --video 5 --action proof --story 1
.\.venv\Scripts\python.exe storyboard_stage.py --video 5 --action status

Çıktılar:

Video 5\04_storyboard\storyboard.json
Video 5\04_storyboard\story_01_30s_proof.mp4
Video 5\05_visuals\generated\01_hero_01.png
Video 5\05_visuals\generated\01_hero_02.png

storyboard.json görsel API çağrısından önce kaydedilir. Aynı prompt/model/ayar ve
geçerli SHA-256 sonucu varsa tekrar API çağrısı yapılmaz. Yanlış boyut veya benzer
görsel reddedilir; kota harcayarak otomatik replacement üretilmez.


13. ESKİ OTOMASYON ÇIKTILARI
-----------------------------

Automation içindeki eski Old ve Video1 klasörleri silinmeden şuraya taşındı:

D:\Dosyalar\İş\YouTube\Tech News\Legacy Automation Outputs

Bu klasör "Video N" biçiminde olmadığı için yeni proje numarasını etkilemez.


14. ÖNEMLİ SINIRLAMALAR
-----------------------

- Ödeme duvarı veya giriş ekranı aşılmaz.
- Okunamayan sayfalarda RSS açıklaması yedek kaynak olarak denenebilir.
- İsim, sayı, tarih ve önemli iddialar yayın öncesinde kaynağa karşı kontrol edilmelidir.
- .env veya API anahtarları Git'e zorla eklenmemelidir.
- Video klasörleri yalnızca "Video N" adıyla Tech News ana klasöründe oluşturulmalıdır.
