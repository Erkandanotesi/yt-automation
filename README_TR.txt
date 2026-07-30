RSS HABER OTOMASYONU — TÜRKÇE KURULUM
=============================================

Bu paket aşağıdaki konuma yerleştirilmek üzere hazırlanmıştır:

D:\Dosyalar\Automation

OLUŞACAK YAPI
--------------

D:\Dosyalar\Automation\
│
├── news_pipeline.py
├── elevenlabs_tts.py
├── setup.bat
├── create_video.bat
├── requirements.txt
├── .env
├── feeds.opml
├── gaming_feeds.opml
├── channel_profile.txt
├── gaming_channel_profile.txt
├── processed_urls.json
│
├── Video1\
│   ├── Haberler\
│   ├── Sesler\
│   ├── aday_haberler.json
│   ├── video_script.txt
│   ├── project_info.json
│   └── pipeline.log
│
└── Video2\ ...


1. DOSYALARI YERLEŞTİR
-----------------------

ZIP dosyasını aç ve içindeki bütün dosyaları şu klasöre kopyala:

D:\Dosyalar\Automation


2. PYTHON KUR
-------------

Bilgisayarında Python 3.11 veya daha yeni bir sürüm bulunmalı.
Python kurulurken "Add Python to PATH" seçeneğini işaretle.


3. KURULUMU ÇALIŞTIR
--------------------

setup.bat dosyasına çift tıkla.

Bu işlem:
- .venv sanal ortamını oluşturur.
- Gerekli Python paketlerini kurar.
- .env dosyasını oluşturur.
- Eksikse feeds.opml ve channel_profile.txt şablonlarını oluşturur.


4. API ANAHTARINI EKLE
----------------------

D:\Dosyalar\Automation\.env dosyasını Not Defteri ile aç.

Gemini için:

AI_PROVIDER=gemini
GEMINI_API_KEY=gerçek_anahtarın
GEMINI_MODEL=gemini-3.6-flash

OpenAI için:

AI_PROVIDER=openai
OPENAI_API_KEY=gerçek_anahtarın
OPENAI_MODEL=gpt-5.6
OPENAI_REASONING_EFFORT=low

Model hesabında kullanılamıyorsa .env içindeki model adını erişebildiğin
başka bir uyumlu model adıyla değiştir.


5. FEEDFLOW OPML DOSYASINI EKLE
-------------------------------

FeedFlow'dan feed listesini OPML olarak dışa aktar.

Dışa aktarılan dosyanın adını:

feeds.opml

yap ve şuraya kaydet:

D:\Dosyalar\Automation\feeds.opml

Paketle gelen boş şablonun üzerine yazmalısın.


6. HER VİDEO İÇİN ÇALIŞTIR
--------------------------

create_video.bat

Dosyaya çift tıkla ve kaynak türünü seç:

1-Tech News -> feeds.opml ve teknoloji promptu
2-Gaming    -> gaming_feeds.opml ve oyun promptu

Program yalnızca ana Automation klasöründeki VideoX klasörlerini kontrol eder,
en büyük numaranın bir sonrasını otomatik oluşturur. Old klasörü
numaralandırmaya dahil edilmez.

ElevenLabs ayarları:

.env dosyasına şu değerleri ekle:

ELEVENLABS_API_KEY_1=BIRINCI_API_ANAHTARIN
ELEVENLABS_API_KEY_2=IKINCI_API_ANAHTARIN
ELEVENLABS_API_KEY_3=UCUNCU_API_ANAHTARIN
ELEVENLABS_VOICE_ID=pNInz6obpgDQGcFmaJgB
ELEVENLABS_MODEL_ID=eleven_multilingual_v2
ELEVENLABS_OUTPUT_FORMAT=mp3_44100_128
ELEVENLABS_SPEED=1.0
ELEVENLABS_STABILITY=0.5
ELEVENLABS_SIMILARITY_BOOST=0.75
ELEVENLABS_STYLE=0.5
ELEVENLABS_SPEAKER_BOOST=true
ELEVENLABS_IS_OVERRIDE=false
ELEVENLABS_LANGUAGE_CODE=en

Test sesi: Adam - Dominant, Firm (premade).
Language Override kapalıdır; API isteğine language_code gönderilmez.

Numaralı API anahtarları sayısal sırayla kullanılır. Bir anahtar geçersizse,
kotası bittiyse veya ödeme/erişim hatası verirse sıradaki anahtara geçilir.
Geçici rate-limit hatalarında aynı anahtar kısa beklemelerle yeniden denenir.
Anahtarların gerçek değerleri log veya tts_info.json dosyasına yazılmaz;
yalnızca kullanılan slot numarası kaydedilir.

Hesabındaki voice ID değerlerini görmek için:

.\.venv\Scripts\python.exe elevenlabs_tts.py --list-voices

create_video.bat önce ElevenLabs anahtarını ve sesi doğrular. Ardından her
özet dosyasını ayrı istekle gönderir. -orijinal.txt dosyaları gönderilmez.
Sesler VideoX\Sesler klasörüne özetlerle aynı temel dosya adıyla kaydedilir.

Önceden hazırlanmış bir videonun seslerini üretmek için:

.\.venv\Scripts\python.exe elevenlabs_tts.py --video 1

Mevcut MP3 dosyalarını yeniden üretmek için:

.\.venv\Scripts\python.exe elevenlabs_tts.py --video 1 --overwrite


7. POWERSHELL ÜZERİNDEN ÇALIŞTIRMA
----------------------------------

cd D:\Dosyalar\Automation

Video 1:
.\.venv\Scripts\python.exe news_pipeline.py --video 1 --category tech

Gaming Video 2:
.\.venv\Scripts\python.exe news_pipeline.py --video 2 --category gaming

OpenAI ile:
.\.venv\Scripts\python.exe news_pipeline.py --video 3 --provider openai

Gemini ile:
.\.venv\Scripts\python.exe news_pipeline.py --video 4 --provider gemini

En fazla 3 haber:
.\.venv\Scripts\python.exe news_pipeline.py --video 5 --top 3

Eski haberleri de tekrar değerlendirmek:
.\.venv\Scripts\python.exe news_pipeline.py --video 6 --reset


8. ÇIKTI DOSYALARI
------------------

Her VideoX klasöründe:

Haberler\01_haber-basligi.txt
- Doğrudan ElevenLabs'e gönderebileceğin sade 150-200 kelimelik metin.
- Başlık, hook, kaynak, puan veya başka bir etiket içermez.

Haberler\01_haber-basligi-orijinal.txt
- Haber animasyonunda kullanmak için çıkarılmış orijinal haber metni.
- Özet dosyasıyla aynı temel dosya adına sahiptir.

video_script.txt
- Seçilen bütün sade seslendirme metinlerinin birleştirilmiş hali.

aday_haberler.json
- RSS taramasında bulunan aday haberler.

project_info.json
- Çalışma özeti ve üretilen dosyalar.

pipeline.log
- Hata ve işlem kayıtları.


9. AYARLAR
----------

.env içindeki önemli değerler:

MAX_AGE_HOURS=36
Yalnızca son 36 saatteki haberleri değerlendirir.

MAX_CANDIDATES=25
AI sıralamasına gönderilecek en fazla aday sayısı.

TOP_N=5

Haber seçimi iki aşamalıdır:

1. Bütün RSS adayları ücretsiz yerel kurallarla puanlanır.
2. En güçlü küçük havuz (TOP_N değerinin yaklaşık üç katı) tek AI çağrısında
   karşılaştırılır. Yalnızca seçilen haberlerin tam metni indirilir ve her
   haber ayrı bir AI çağrısında 150-200 kelimelik metne dönüştürülür.

Bu yöntem, bütün tam haberleri aynı anda modele göndermekten daha ucuz ve
özellikle gpt-5.6-luna gibi yüksek hacimli kullanıma uygun modellerde daha
tutarlıdır.
Video için üretilecek en fazla haber sayısı.

MIN_SCORE=65
65 puanın altındaki haberleri özetlemez.

MIN_ARTICLE_CHARS=500
Tam haber metninin yeterli kabul edilmesi için gereken karakter sayısı.


10. AYNI HABERİN TEKRARLANMASI
------------------------------

Başarıyla dosyaya kaydedilen haber URL'leri şu dosyada tutulur:

D:\Dosyalar\Automation\processed_urls.json

Video2 hazırlanırken Video1'de kullanılan haberler otomatik olarak atlanır.

Geçmiş haberleri yalnızca bir çalışmada yeniden değerlendirmek için:

news_pipeline.py --video X --reset

komutunu kullan. Bu seçenek processed_urls.json dosyasını silmez.


11. KANAL TERCİHLERİNİ DEĞİŞTİR
-------------------------------

D:\Dosyalar\Automation\channel_profile.txt

dosyasını düzenleyerek hangi haberlerin yüksek veya düşük öncelikli olduğunu
değiştirebilirsin. Bu metin AI haber seçim prompt'una otomatik eklenir.


12. ÖNEMLİ SINIRLAMALAR
-----------------------

- Ödeme duvarı veya giriş ekranı aşılmaz.
- Haber sayfası okunamazsa RSS açıklaması kullanılmaya çalışılır.
- AI özetini videoya koymadan önce isimleri, sayıları, tarihleri ve önemli
  iddiaları kaynağa karşı manuel kontrol et.
- ChatGPT/Gemini web uygulamasına giriş yapmak yeterli değildir; ilgili API
  anahtarı .env dosyasına eklenmelidir.
