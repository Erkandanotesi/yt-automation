NEWS STUDIO OTOMASYONU — TÜRKÇE KULLANIM
=========================================

1. KLASÖR DÜZENİ
----------------

Ana çalışma klasörü:

D:\Dosyalar\İş\YouTube\Tech News

Automation yalnızca kod, ayar, bağımlılık ve otomasyon durum verilerini tutar:

Tech News\
├── Automation\
│   ├── .env
│   ├── create_video.bat
│   ├── news_pipeline.py
│   ├── elevenlabs_tts.py
│   ├── preflight.bat
│   ├── processed_urls.json
│   └── ...
├── Media\
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

Yeni seslerde ElevenLabs "with timestamps" yanıtı kullanılacaktır. Bu yanıtın
karakter zamanları kelime, cümle, görsel değişim noktaları ve SRT/ASS altyazıya
dönüştürülecektir.

Daha önce üretilmiş MP3 dosyaları ElevenLabs Forced Alignment API ile mevcut
onaylı metne hizalanacaktır. Böylece altyazı metni tekrar transkripsiyonla
değişmez.

Bu zamanlama aşaması Milestone 2'de uygulanacaktır.


11. ESKİ OTOMASYON ÇIKTILARI
-----------------------------

Automation içindeki eski Old ve Video1 klasörleri silinmeden şuraya taşındı:

D:\Dosyalar\İş\YouTube\Tech News\Legacy Automation Outputs

Bu klasör "Video N" biçiminde olmadığı için yeni proje numarasını etkilemez.


12. ÖNEMLİ SINIRLAMALAR
-----------------------

- Ödeme duvarı veya giriş ekranı aşılmaz.
- Okunamayan sayfalarda RSS açıklaması yedek kaynak olarak denenebilir.
- İsim, sayı, tarih ve önemli iddialar yayın öncesinde kaynağa karşı kontrol edilmelidir.
- .env veya API anahtarları Git'e zorla eklenmemelidir.
- Video klasörleri yalnızca "Video N" adıyla Tech News ana klasöründe oluşturulmalıdır.
