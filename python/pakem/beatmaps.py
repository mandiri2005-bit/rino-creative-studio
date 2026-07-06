"""python/pakem/beatmaps.py — Beat-map library v2 (romance + K-drama + recombination).

Structure layer for narasi outline generation. Beat-map governs PLOT ARCHITECTURE,
orthogonal to `style` (register). Selected via mode:
  - preset (default): one of the 26 authored presets (10 romance + 12 kdrama + 4 v2 portfolio).
  - compose (romance only): free-composed from axis fragments (arc × conflict × resolution × pov),
    pruned by validity matrix (~1400 valid combos in romance).
  - off: no beat-map.

TWIST layer (v2): 6 twist primitives roll independently per narasi. 60% of narasi ship
with no twist (weighted default). Beat-maps with a signature twist skip the layer.
Anti-repeat: separate Redis list `beatmap:recent_twist:{tenant}`.

Flag-gated on DALANG_BEATMAP_ENABLED (default OFF, byte-identical).

DO NOT hand-edit authored strings — regenerate via scratchpad/gen_beatmaps_v3.py from
the workflow result JSON files.
"""
from __future__ import annotations

import os
import re
import random
from typing import Optional


def beatmaps_enabled() -> bool:
    return os.environ.get("DALANG_BEATMAP_ENABLED") == "1"


_ROMANCE_STYLES = frozenset({
    "remaja_coming_of_age", "coming_of_age",
    "romance_contemporary", "romance",
})
_KDRAMA_STYLES = frozenset({
    "kdrama_serial", "kdrama",
})


def beatmap_family_for_style(style: Optional[str]) -> Optional[str]:
    s = (style or "").strip().lower()
    if s in _ROMANCE_STYLES:
        return "romance"
    if s in _KDRAMA_STYLES:
        return "kdrama"
    return None


BEAT_MAPS = {
    "linear_confession": {
        "display_name": "Linear Confession",
        "family": "romance",
        "tone": "earnest",
        "axes": {"arc": "linear", "conflict": "internal_fear", "resolution": "grand_gesture_then_growth", "pov": "first", "twist": "none"},
        "structural_prompt": "Tulis kisah lini-masa lurus (kronologis, tanpa kilas-balik atau lompatan waktu) dengan konflik INTERNAL murni: seluruh penghalang bukan orang lain, keadaan, atau salah paham, melainkan satu keyakinan-diri spesifik yang tokoh utama pegang tentang dirinya (mis. \"aku selalu jadi pilihan kedua\", \"orang sepertiku tak layak dipilih\", \"kalau aku jujur, semua rusak\"). Namai keyakinan itu di bab awal dan jadikan ia MESIN cerita: tiap bab satu retakan pada keyakinan tersebut, bukan satu langkah mendekat secara fisik.\n\nBUKA di tengah kedekatan yang SUDAH ada — tokoh utama dan orang yang ia taksir telah saling kenal/dekat di latar yang ditentukan user. DILARANG membuka dengan perkenalan canggung, papan pengumuman, atau pertemuan-kebetulan. Titik-nol = tokoh sadar rasa itu ada TAPI yakin dirinya tak boleh/tak bisa mengungkapkannya karena keyakinan tadi.\n\nBELOK lewat perubahan-dalam, bukan peristiwa-luar: keyakinan diuji oleh pilihan kecil yang jujur, gagal, lalu tokoh melihat bukti bahwa keyakinannya keliru. Eskalasi = keberanian internal menaik, bukan kecemburuan, pihak ketiga, atau tekanan keluarga.\n\nMENDARAT dengan pertumbuhan yang MEMUNGKINKAN satu gestur pengakuan yang menentukan dan berisiko — tokoh sengaja mempertaruhkan citra-aman dirinya secara terbuka pada lawan, akibat wajar dari perubahan, bukan tujuan yang dikejar sejak awal. Gestur itu nyata dan berani, TAPI bukan panggung, bukan surat dramatis, bukan tontonan demonstratif untuk penonton. Sesudahnya, tunjukkan bagaimana cara tokoh memandang diri berubah, apa pun jawaban lawan.\n\nPOV KUNCI: orang-pertama \"aku\" di SETIAP bab tanpa kecuali; batin tokoh adalah alat utama. Jangan pernah berpindah ke sudut pandang lawan.\n\nDILARANG KERAS: tukar benda kecil (pulpen/bekal/tumbler), metafora orbit/gravitasi, hitung \"hari ke-N\", label friendzone, telepon orang tua soal nilai, baca ulang chat seperti puisi, alam-sebagai-saksi (genangan/pohon) di penutup, dan aforisme \"keberanian bukan absennya rasa takut\".",
        "chapter_spine": ["Bab 1 — Kedekatan yang sudah ada dinamai; tokoh utama menyebut keyakinan-diri yang mengunci rasanya", "Bab 2 — Keyakinan itu diuji pertama kali oleh pilihan jujur kecil; tokoh mundur, membenarkan diri", "Bab 3 — Bukti pertama bahwa keyakinannya keliru muncul dari dalam interaksi biasa; tokoh menolak percaya", "Bab 4 — Tokoh menguji ulang dirinya dengan sengaja, gagal setengah, tapi retakan keyakinan melebar", "Bab 5 — Perubahan matang jadi satu gestur pengakuan yang menentukan dan berisiko; tokoh mempertaruhkan citra-aman secara terbuka, bukan lewat panggung atau surat", "Bab 6 — Dampak pengakuan pada cara tokoh memandang diri; pertumbuhan berdiri terlepas dari jawaban lawan"],
    },
    "breakup_first": {
        "display_name": "Breakup-First",
        "family": "romance",
        "tone": "melancholic",
        "axes": {"arc": "non_linear", "conflict": "misunderstanding", "resolution": "open_ended", "pov": "retrospective_first", "twist": "none"},
        "structural_prompt": "STRUKTUR CERITA (WAJIB — bentuk arsitektur plot, bukan gaya bahasa):\n\nBUKA DARI AKHIR. Bab pembuka berlangsung SETELAH hubungan tokoh utama dan orang yang ia taksir sudah berakhir/berpisah. Perlihatkan sisa-sisanya lebih dulu (jarak yang sekarang ada, satu benda/tempat/kebiasaan yang tertinggal), bukan pertemuan pertama. Pembaca langsung tahu HASILNYA — ketegangan cerita adalah KENAPA, bukan APAKAH mereka jadi.\n\nLALU REKONSTRUKSI LEWAT KILAS BALIK. Bab-bab berikut adalah kenangan tokoh utama yang menata ulang bagaimana kedekatan tumbuh dan bagaimana ia retak. Susun kilas balik supaya SATU SALAH PAHAM inti (sesuai tema yang ditentukan user) baru benar-benar terurai di depan pembaca lewat potongan kenangan — sesuatu yang dulu tak terbaca tokoh utama, kini terbaca saat dikenang. Salah paham ini BUKAN cemburu-remeh atau salah tokoh utama sendiri; ia lahir dari dua hal yang tak sempat dikatakan/didengar pada latar yang ditentukan user.\n\nDARAT SECARA TERBUKA. Jangan tutup dengan rujuk, confession panggung, atau surat besar. Kembali ke waktu-sekarang pembukaan; tokoh utama kini MENGERTI kenapa, tapi keadaan tetap tak dipulihkan. Biarkan menggantung: satu kemungkinan yang tak dijawab, bukan reuni. Pemahaman adalah penutupannya.\n\nPOV DIKUNCI: orang-pertama retrospektif (aku, mengenang dari kini) di SEMUA bab — kilas balik pun tetap diceritakan sebagai kenangan, bukan waktu-nyata. Jangan pindah ke sudut pandang orang yang ia taksir.\n\nDILARANG: buka dengan meet-cute/papan pengumuman/kenalan gugup; ritme orbit→tukar benda kecil→confession→ending-tumbuh; salah paham yang salah tokoh utama sendiri; tutup dengan grand-gesture atau rujuk. Hindari juga klise mikro (satu earphone di hujan, metafora orbit/gravitasi, telepon soal nilai, baca-ulang chat, aforisme keberanian). Resolusi open-ended, tanpa twist pembalik.",
        "chapter_spine": ["Bab1 — Buka di waktu-kini: sisa perpisahan, satu benda/tempat yang tertinggal; pembaca tahu sudah berakhir", "Bab2 — Kilas balik mulai: kenangan awal kedekatan, ditata ulang oleh tokoh utama dari kini", "Bab3 — Kilas balik: momen paling dekat, tapi diselipi benih hal yang tak sempat terucap", "Bab4 — Kilas balik: adegan salah paham inti terjadi — dulu tak terbaca tokoh utama", "Bab5 — Kilas balik menutup: potongan yang hilang terurai; alasan sebenarnya perpisahan terlihat", "Bab6 — Kembali ke waktu-kini: tokoh utama kini mengerti kenapa, keadaan tetap tak pulih, satu kemungkinan menggantung tanpa jawaban"],
    },
    "dual_pov_parallel": {
        "display_name": "Dual-POV Parallel",
        "family": "romance",
        "tone": "bittersweet",
        "axes": {"arc": "dual_pov", "conflict": "miscommunication", "resolution": "quiet_convergent", "pov": "dual_alternating", "twist": "none"},
        "structural_prompt": "Bangun cerita dua sudut orang-pertama BERGANTIAN ketat: bab ganjil suara \"tokoh utama\", bab genap suara \"orang yang ia taksir\". POV terkunci sepanjang buku — tiap bab HANYA satu kepala, tanpa akses ke isi kepala satunya. Mesin utama = ironi dramatis: hanya PEMBACA yang memegang dua sisi; kedua tokoh selalu bergerak dari data separuh.\n\nBUKA (Bab 1-2): jangan meet-cute klasik. Keduanya sudah saling kenal/terlibat di latar yang ditentukan user. Sajikan SATU momen yang sama dari dua bacaan yang bertolak belakang — apa yang satu anggap penolakan, satunya anggap perlindungan. Sejak awal pembaca tahu lebih banyak dari keduanya.\n\nPUTAR (tengah): jurang melebar BUKAN lewat cemburu/pihak ketiga/salah-sendiri. Tiap bab, satu tokoh menafsir sinyal netral secara masuk akal tapi keliru; bab pasangannya membongkar niat asli di baliknya. Susun 2-3 \"titik lewat\" (near-miss): mereka nyaris meluruskan tapi meleset karena beda kosakata batin. Setiap sisi rasional; sakitnya datang karena pembaca lihat dua kebenaran sekaligus. Konflik inti wajib menyesuaikan tema user.\n\nMENDARAT (2 bab akhir): quiet-convergent — DILARANG confession panggung, surat tangan, atau grand-gesture. Satu kalimat jujur kecil ATAU satu tindakan biasa menutup celah. Bab kedua-dari-akhir dan bab terakhir memandang OBJEK/MOMEN yang sama — kini terbaca sama oleh keduanya. Dua garis batin akhirnya bertemu di titik yang sejak awal pembaca tunggu. Tumbuh sunyi, bukan meledak.\n\nJaga tiap bab benar-benar buta terhadap isi bab sebelahnya; jangan bocorkan satu POV ke POV lain. Hindari tukar-benda-kecil, orbit/gravitasi, ibu-telepon-soal-nilai, dan aforisme keberanian.",
        "chapter_spine": ["Bab 1 (POV A): satu momen ditafsir sebagai penolakan — pembaca sudah curiga A keliru", "Bab 2 (POV B): momen yang sama, niat asli terungkap — celah pertama menganga bagi pembaca", "Bab 3 (POV A): A bertindak rasional atas data separuh, menjauh; near-miss #1 meleset", "Bab 4 (POV B): B salah-baca sinyal netral A; dua kebenaran paralel makin lebar", "Bab 5 (POV A): near-miss #2 — nyaris jujur, tersandung beda kosakata batin; objek-poros muncul", "Bab 6 (POV B): satu kalimat/tindakan biasa menutup celah; objek yang sama kini terbaca sama — dua garis bertemu"],
    },
    "slow_fade": {
        "display_name": "The Slow Fade",
        "family": "romance",
        "tone": "melancholic",
        "axes": {"arc": "linear", "conflict": "timing_erosion", "resolution": "un_healed", "pov": "first", "twist": "none"},
        "structural_prompt": "STRUKTUR CERITA — \"The Slow Fade\". Tokoh utama dan orang yang ia sayangi SUDAH bersama sejak awal; jangan buka dengan perkenalan, meet-cute, papan pengumuman, atau fase pdkt. Bab pembuka memotret hubungan yang sedang berjalan dan tampak baik-baik saja — kilas \"dulu\" boleh disebut sekilas sebagai latar, bukan dijadikan babak. Konflik inti bukan satu pertengkaran, bukan pihak ketiga, bukan salah paham, bukan cemburu: yang mengikis adalah waktu dan jarak kecil yang menumpuk — jeda balasan memanjang, rencana batal tanpa ribut, kalimat yang tidak jadi diucapkan, ritme dua orang yang perlahan tak lagi bertemu, sesuai tema user. Arahnya LINEAR: tiap bab menaikkan keheningan satu takik, tak pernah membaik lalu memburuk lagi — makin sedikit yang diceritakan, makin banyak yang dianggap wajar. Keduanya saling berpura-pura tak apa; narator menyadari retak tapi merasionalisasi (\"mungkin cuma capek\", \"nanti membaik\"). TITIK BALIK bukan ledakan — satu momen sunyi saat narator sadar kehilangan sudah terjadi diam-diam. AKHIR wajib un-healed dan getir-manis: memudar, bukan putus dramatis; tutup dengan perpisahan lembut atau kebersamaan yang tinggal cangkang — hangat sekaligus kehilangan, tanpa pemenang, penyelamat, atau twist. POV KUNCI orang-pertama (aku) di SEMUA bab tanpa berpindah; batin narator yang setengah sadar dan membenarkan diri adalah mesin cerita. DILARANG: confession panggung, surat besar, grand-gesture, rekonsiliasi, adegan putus meledak, villain. DILARANG micro-klise: metafora orbit/gravitasi, berbagi satu earphone saat hujan, telepon ibu soal nilai, membaca ulang chat lama seolah puisi, cuaca/musim sebagai cermin perasaan, aforisme penutup gaya motivasi. Jangan menjelaskan istilah atau menyitir data; biarkan kehilangan tersirat.",
        "chapter_spine": ["Bab1 — potret hubungan yang sudah berjalan dan tampak utuh; keakraban sebagai titik awal, bukan tujuan", "Bab2 — retak halus pertama: jeda balasan memanjang, satu rencana batal tanpa ribut, dianggap wajar", "Bab3 — keheningan menumpuk; makin banyak yang tak diceritakan, narator mulai merasionalisasi", "Bab4 — dua orang saling berpura-pura baik-baik saja; ritme hidup mereka tak lagi bertemu", "Bab5 — titik balik sunyi: narator sadar kehilangan sudah terjadi diam-diam, tanpa pertengkaran", "Bab6 — memudar getir-manis: perpisahan lembut atau kebersamaan tinggal cangkang, un-healed, tanpa penyelamat"],
    },
    "secret_reveal": {
        "display_name": "Secret / Reveal",
        "family": "romance",
        "tone": "bittersweet",
        "axes": {"arc": "linear", "conflict": "secret", "resolution": "recontextualize", "pov": "first", "twist": "one_was_leaving"},
        "structural_prompt": "Bangun kisah LINEAR ber-POV ORANG-PERTAMA (aku), TERKUNCI di sudut pandang tokoh utama sepanjang SEMUA bab — tokoh utama TIDAK tahu rahasianya. Mesin cerita: satu RAHASIA yang dipegang orang yang ia taksir (ia akan pergi / menyembunyikan sesuatu tentang dirinya) yang, saat terungkap di TITIK-TENGAH, MENATA ULANG MAKNA semua adegan sebelumnya.\n\nBUKA bukan dengan perkenalan/meet-cute: mulai dari kedekatan yang SUDAH terjalin, lalu tanam 3-4 DETIL GANJIL yang di pembacaan pertama tampak wajar/manis (jawaban yang menghindar, janji yang selalu ditunda ke \"nanti\", benda/kebiasaan yang tak dijelaskan, momen ia menatap seolah menghafal). Detil ini WAJIB ditanam SEBELUM titik-tengah agar bisa dibaca ulang — bukan kejutan murahan.\n\nBELOK di TITIK-TENGAH: rahasia terungkap. Sejak bab itu, tokoh utama (dan pembaca) MEMBACA ULANG masa lalu — setiap detil ganjil kini punya arti kedua. Bab-bab setelahnya adalah kerja REKONTEKSTUALISASI: bukan konfrontasi/cemburu, melainkan menimbang ulang setiap kenangan di bawah cahaya baru.\n\nMENDARAT dengan REKONTEKSTUALISASI, bukan grand-gesture: penutup adalah pemahaman yang berubah — makna hubungan dinilai ulang, bukan panggung pengakuan atau surat besar. Boleh berpisah, boleh tinggal, tapi yang berubah adalah CARA memaknai, bukan status.\n\nKonflik inti mengikuti tema yang diberi user; latar mengikuti latar yang ditentukan user. Rahasia harus organik terhadap tema itu. Larang: aparat klise (papan pengumuman, tukar benda kecil, telepon ibu soal nilai), metafora orbit/gravitasi, konfrontasi salah-paham/cemburu sebagai poros, dan aforisme keberanian. POV tak boleh berpindah ke orang lain — rahasia HANYA terungkap dari sisi tokoh utama.",
        "chapter_spine": ["Bab1 — Kedekatan yang sudah terjalin; tanam detil ganjil pertama yang terbaca manis", "Bab2 — Rutinitas bersama menumpuk; 2-3 keganjilan lagi (janji ditunda, jawaban menghindar) tampak wajar", "Bab3 — Firasat halus tokoh utama; satu keganjilan nyaris terbongkar lalu tertutup lagi", "Bab4 — TITIK-TENGAH: rahasia terungkap; makna semua adegan sebelumnya runtuh dan tertata ulang", "Bab5 — Membaca ulang kenangan di bawah arti baru; menimbang tiap detil yang dulu terlewat", "Bab6 — Mendarat dengan pemahaman yang berubah (rekontekstualisasi), bukan pengakuan panggung"],
    },
    "rival_to_love": {
        "display_name": "Rival-to-Love",
        "family": "romance",
        "tone": "warm",
        "axes": {"arc": "linear", "conflict": "rivalry", "resolution": "earned", "pov": "first", "twist": "none"},
        "structural_prompt": "Bangun kisah ini sebagai rivalitas nyata yang perlahan menjadi cinta, dituturkan konsisten dari sudut pandang orang pertama (aku) di SETIAP bab tanpa berpindah. BUKA langsung di tengah kompetisi yang sudah berjalan: tokoh utama dan orang yang jadi lawannya sudah saling kenal sebagai saingan dalam arena yang ditentukan tema user (lomba, perebutan posisi, target yang sama, klien yang sama, dsb). Dilarang membuka dengan meet-cute klasik: tidak ada papan pengumuman, kenalan gugup, atau pertemuan tak sengaja. Antagonisme harus SAH dan berbalas dua arah, lahir dari taruhan konkret yang sama-sama diperebutkan, BUKAN dari salah paham. Naikkan tekanan lewat babak-babak kompetisi yang makin sengit; ketertarikan tumbuh JUSTRU dari rivalitas, bukan meski ada salah paham. Titik balik = satu momen tokoh utama menyaksikan kompetensi, integritas, atau pengorbanan sang lawan dari dekat, lalu respek menggantikan permusuhan; sejak sini keduanya jadi lawan sekaligus sekutu. Kompetisi harus benar-benar DIPUTUSKAN: ada yang menang, kalah, seri, atau taruhannya berubah bentuk, dan hasil itu diterima jujur. Cinta muncul SETELAH respek terbangun dan bertahan melewati hasil itu — earned, bukan grand-gesture. DILARANG: beat salah-paham/cemburu sebagai penggerak, orbit tukar benda kecil (pulpen/tumbler/bekal), tukar kontak setelah N hari, tekanan ibu-soal-nilai, pengakuan panggung atau surat tangan sebagai klimaks, dan penutup alam-sebagai-saksi (genangan aspal/pohon). Tutup dengan pengakuan yang tenang dan setara antara dua orang yang sudah saling menghormati sebagai penantang sepadan, bukan gestur besar. Jaga taruhan kompetisi tetap hidup sampai bab akhir; jangan biarkan rivalitas menguap jadi manis tanpa keputusan.",
        "chapter_spine": ["Bab 1 — Arena sudah panas: aku dan saingan bertemu di tengah kompetisi berjalan, taruhan konkret ditegaskan (bukan perkenalan)", "Bab 2 — Ronde pertama: kami saling ukur kekuatan, aku meremehkan lawan; permusuhan berbalas dua arah mengeras", "Bab 3 — Tekanan naik: babak yang lebih sengit memaksa kami berdekatan, celah pertama pada anggapanku soal dia mulai muncul", "Bab 4 — Titik balik: aku menyaksikan kompetensi/integritas/pengorbanan lawan dari dekat, respek menggantikan permusuhan", "Bab 5 — Keputusan: kompetisi benar-benar diputuskan (menang/kalah/seri), hasil diterima jujur; respek bertahan melewati hasilnya", "Bab 6 — Pengakuan setara: cinta yang earned diakui dengan tenang antara dua penantang sepadan, tanpa gestur besar"],
    },
    "second_chance": {
        "display_name": "Second Chance",
        "family": "romance",
        "tone": "bittersweet",
        "axes": {"arc": "dual_timeline", "conflict": "unfinished_history", "resolution": "reconcile_or_release", "pov": "first", "twist": "none"},
        "structural_prompt": "Bangun kisah dwi-lini-waktu: jalin dua garis — MASA LALU (hubungan tokoh utama dengan orang itu yang dulu retak) dan KINI (mereka berjumpa lagi di latar yang ditentukan user). Wajib POV orang-pertama (aku/gue) TERKUNCI di kedua lini; jangan pindah ke sudut pandang orang lain di bab mana pun.\n\nBUKA di KINI, pada momen perjumpaan-ulang yang tak diminta — dua orang yang SUDAH saling kenal, bukan asing. Dilarang buka dengan meet-cute klasik (papan pengumuman, kenalan gugup, tukar kontak setelah N hari): sejarah sudah ada sejak kalimat pertama; yang belum ada adalah penjelasannya.\n\nBELAH tiap bab (atau selang-seling antar bab) antara KINI dan satu keping MASA LALU. Setiap keping masa lalu harus MENJELASKAN satu bagian luka di masa kini — susun agar pembaca paham APA yang dulu terjadi hanya secara bertahap, menahan penyebab retaknya sampai mendekati titik-balik. Konflik inti = sejarah yang belum selesai (sesuai tema user): bukan salah-paham baru, melainkan hal lama yang tak pernah dibereskan — kata yang tak terucap, pilihan yang menyakiti, perpisahan tanpa penutup.\n\nBELOK di tengah: keping masa lalu terakhir membongkar penyebab retak yang sebenarnya, mengubah cara pembaca (dan tokoh utama) membaca kini. Jangan pakai twist pihak-ketiga atau rahasia besar; belokannya adalah KEBENARAN yang selama ini ditahan, bukan kejutan plot.\n\nDARAT di persimpangan reconcile-ATAU-release: tokoh utama memilih BERBAIKAN (memulai ulang dengan mata terbuka) ATAU MERELAKAN dengan bersih (berdamai tanpa bersama lagi). Pilih satu dengan tegas sesuai bobot cerita. Bila jatuh ke release, DILARANG tutup dengan grand-gesture/pengakuan panggung/surat besar — tutup sunyi, dewasa, tanpa balikan paksa. Kedua lini bertemu di bab akhir: masa lalu ditutup, kini menemukan bentuknya.\n\nLarangan klise: jangan tukar benda kecil (pulpen/bekal/tumbler), berbagi satu earphone di hujan, metafora orbit/gravitasi, ibu menelepon soal nilai, aforisme \"keberanian bukan absennya rasa takut\", alam-sebagai-saksi. Sejarah dua orang ini yang jadi mesin cerita, bukan micro-beat manis.",
        "chapter_spine": ["KINI: perjumpaan-ulang tak terduga — sejarah terasa, penyebabnya belum", "MASA LALU: kepingan awal kebersamaan yang dulu — akar ikatan ditanam", "KINI: canggung berlanjut, luka lama muncul tanpa nama yang jelas", "MASA LALU: retaknya mulai terbaca — tapi penyebab sejati masih ditahan", "TITIK-BALIK: keping masa lalu terakhir membongkar kebenaran yang selama ini disimpan", "KINI (persimpangan): berbaikan dengan mata terbuka ATAU merelakan bersih — dua lini ditutup"],
    },
    "the_almost": {
        "display_name": "The Almost",
        "family": "romance",
        "tone": "melancholic",
        "axes": {"arc": "linear", "conflict": "timing", "resolution": "un_healed", "pov": "retrospective_first", "twist": "none"},
        "structural_prompt": "STRUKTUR CERITA — \"The Almost\" (linear retrospektif, konflik timing, akhir un-healed).\n\nINTI: hubungan tokoh utama dengan orang yang ia taksir TAK PERNAH benar-benar mulai. Sepanjang cerita mereka nyaris — berulang kali mendekati satu garis dan tak pernah melewatinya. Bukan putus, bukan pudar sambil bersama; ini yang-nyaris yang tak jadi.\n\nPOV DIKUNCI: orang-pertama retrospektif (\"aku\", suara yang sudah tahu bagaimana ini berakhir) di SETIAP bab. Tokoh bercerita dari SEKARANG tentang MASA LALU; jangan pernah pindah ke orang ketiga atau ke sudut orang yang ia taksir.\n\nBUKA: dari sekarang, dari sisa rasa. Sebuah pemicu di masa kini (kabar, tempat, benda, tanggal) membuka ingatan. DILARANG buka dengan meet-cute klasik (papan pengumuman/kenalan gugup); pertemuan pertama, kalau ada, muncul sebagai kilas balik yang SUDAH diberi bayangan akhir.\n\nBADAN — mesin plot: rangkaian AMBANG yang didekati lalu urung. Tiap bab = satu momen \"hampir jadi\" yang digagalkan oleh WAKTU, bukan oleh salah paham atau cemburu diri sendiri: salah satu sudah bersama orang lain, salah satu akan pergi, salah satu ragu sedetik terlalu lama, kesempatan lewat sebelum terucap. Naikkan taruhannya lewat makin dekat–makin mustahil, bukan lewat drama eksternal. DILARANG: tukar-benda-kecil sebagai denyut cerita, telepon-ibu-soal-nilai, friendzone eksplisit, metafora orbit/gravitasi.\n\nTUTUP: un-healed. Garis itu TAK PERNAH dilewati — tanpa confession panggung, tanpa surat, tanpa reuni, tanpa grand-gesture. Bila resolusi un-healed, grand-gesture DILARANG. Tokoh menutup dari sekarang: menerima bahwa yang-nyaris tetap nyaris; rindunya tinggal, tidak sembuh, tidak jadi pelajaran manis. Boleh tenang, tidak boleh utuh.\n\nSesuaikan setiap ambang dengan latar dan konflik inti sesuai tema yang ditentukan user. Jangan sitir statistik, jangan definisikan istilah umum.",
        "chapter_spine": ["Bab1 — Dari sekarang: satu pemicu membuka ingatan tentang orang yang tak pernah jadi milik siapa-siapa (frame retrospektif, akhir sudah dibayangi)", "Bab2 — Ambang pertama: mereka nyaris, tapi waktunya belum — salah satu masih terikat/tak siap; garis tak terlewati", "Bab3 — Makin dekat: satu momen paling mungkin, terhalang keadaan (salah satu akan pergi / kesempatan lewat sebelum terucap)", "Bab4 — Salah-tempo puncak: yang satu siap justru saat yang lain sudah tak bisa; keduanya tahu tapi diam", "Bab5 — Perpisahan tanpa nama: berpisah tanpa pernah menyebut apa mereka; tak ada confession, tak ada penutupan", "Bab6 — Kembali ke sekarang: garis itu tak pernah dilewati; rindu diterima apa adanya, tidak sembuh, tidak jadi hikmah"],
    },
    "ambition_collision": {
        "display_name": "Ambition Collision",
        "family": "romance",
        "tone": "bittersweet",
        "axes": {"arc": "linear", "conflict": "ambition_clash", "resolution": "choose_and_lose", "pov": "first", "twist": "none"},
        "structural_prompt": "STRUKTUR CERITA — \"Tabrakan Ambisi\". Arsitektur linear, tapi WAJIB menyimpang dari kerangka romansa baku. Konflik intinya BUKAN rasa takut atau salah paham: cinta antara tokoh utama dan orang yang ia taksir itu NYATA dan disadari sejak awal. Yang tak muat adalah dua jalan hidup — dua panggilan/karier/kota/impian yang tak bisa dijalani sekaligus. Tak ada pihak yang salah; dua mimpi itu sama-sahnya. POV kunci di orang-pertama (aku/gue) dari awal sampai akhir, tanpa pindah ke sudut pandang lain.\n\nBUKA: jangan meet-cute. Bond sudah hidup, atau terjalin cepat lewat satu arena bersama sesuai tema user. Sejak bab pertama, beri KEDUANYA satu \"arah/utara\" masing-masing yang sudah jelas namanya (cita-cita, jalur, tujuan) — benih tabrakan ditanam di sini, bukan ditunda ke bab tengah.\n\nBELOK (titik tengah): dua jalan itu terbukti saling meniadakan lewat satu percabangan KONKRET sesuai konflik tema (tawaran, penerimaan, penempatan, tenggat, keberangkatan). Bukan cemburu, bukan miskomunikasi — realita yang dingin: kalau satu diambil, yang lain gugur.\n\nMENDARAT: choose-and-lose. Ada yang MEMILIH, dan ada yang KEHILANGAN — sebut kehilangannya secara nyata. Dilarang menutup dengan grand-gesture yang tiba-tiba membuat dua mimpi muat berdua, atau kompromi ajaib yang menghapus harga. Boleh pahit-manis; yang WAJIB: pilihan itu memakan sesuatu yang nyata.\n\nJANGAN pakai: tukar benda kecil sebagai perekat, kata tersangkut/friendzone, tekanan-ibu-soal-nilai, salah paham/cemburu sebagai mesin cerita, rekonsiliasi panggung/surat tangan, penutup \"alam jadi saksi\", metafora orbit/gravitasi, atau aforisme keberanian. Jangan sisipkan definisi istilah, statistik, atau kutipan survei.",
        "chapter_spine": ["Dua arah tergambar sejak awal — dua orang dekat, dua panggilan yang diam-diam tak searah", "Kedekatan makin nyata, tapi masing-masing punya 'utara' yang tak bisa ditawar", "Satu peluang besar datang untuk salah satu — percabangan mulai berbentuk nyata", "Kedua jalan terbukti tak muat berdua — hitungan yang tak punya jalan menang", "Keputusan diambil — ada yang memilih, ada yang merelakan", "Hidup sematang pilihan — apa yang dibawa pergi, apa yang ditinggalkan"],
    },
    "group_to_pair": {
        "display_name": "Group-to-Pair",
        "family": "romance",
        "tone": "warm",
        "axes": {"arc": "linear", "conflict": "social_cost", "resolution": "quiet", "pov": "first", "twist": "none"},
        "structural_prompt": "STRUKTUR CERITA — Group-to-Pair: alur linear, konflik biaya-sosial, pendaratan tenang, POV orang-pertama (\"aku\") terkunci di SEMUA bab (jangan pernah masuk kepala anggota lain; mereka dikenali lewat ucapan dan reaksi yang terlihat, bukan isi hati). Tokoh utama dan orang yang ia taksir SUDAH satu lingkaran pertemanan sejak cerita dibuka — bukan orang asing. DILARANG meet-cute (papan pengumuman, kenalan gugup, tabrakan tak sengaja). Buka di tengah dinamika grup yang sudah hangat — satu adegan kolektif (sesuai latar yang ditentukan user) saat \"aku\" sadar rasanya ke satu orang mulai beda dari ke yang lain. Konflik inti = BIAYA SOSIAL, bukan restu orang tua, nilai, atau minder. Taruhannya lingkaran itu sendiri: bila rasa ini keluar lalu gagal, grup yang jadi rumah bisa retak. DILARANG tiga tekanan klise (ibu telepon soal nilai, ragu-diri murni). Friksi tengah harus lahir DARI grup: pergeseran aliansi, satu anggota merasa tertinggal, memihak berarti mengorbankan yang lain — BUKAN cemburu satu-lawan-satu. Titik balik = keputusan sunyi. DILARANG confession di depan orang banyak, surat dramatis, gestur besar. Pasangan terbentuk lewat satu percakapan kecil dan privat; grup TIDAK bubar, ia menata ulang bentuk. HINDARI juga micro-beat pipeline: tukar-benda-kecil (pulpen/bekal/tumbler), berbagi satu earphone di hujan, rambut \"diikat asal\", baca ulang chat seperti puisi, metafora orbit/gravitasi, hujan/alam-sebagai-saksi, aforisme keberanian. Tutup tenang: keadaan baru yang lebih jujur. Jaga tekstur ensemble — anggota lain punya nama, suara, reaksi, bukan latar.",
        "chapter_spine": ["Bab1 — Grup sudah utuh: 'aku' di tengah rutinitas circle, sadar satu orang mulai terasa beda", "Bab2 — Rasa menguat diam-diam; 'aku' menimbang apa yang bisa hilang kalau grup tahu (tanpa tukar-benda, tanpa metafora orbit)", "Bab3 — Anggota lain mulai membaca situasi; keseimbangan pertemanan goyah, canggung menjalar ke seluruh grup", "Bab4 — Memihak berarti mengorbankan yang lain; satu anggota merasa tertinggal, grup nyaris retak", "Bab5 — Percakapan kecil dan privat: pengakuan tenang tanpa panggung, tanpa surat, tanpa gestur besar", "Bab6 — Grup menata ulang bentuknya, tidak bubar; keadaan baru yang lebih jujur, pendaratan sunyi tanpa alam-sebagai-saksi"],
    },
    "chaebol_contract": {
        "display_name": "Chaebol Contract",
        "family": "kdrama",
        "tone": "rom_com_to_melodrama",
        "axes": {"arc": "linear", "a_conflict": "class_divide", "b_plot": "corporate_power", "twist": "chaebol_succession", "resolution": "earned_union_after_fallout", "tone": "rom_com_to_melodrama", "scale": "corporate"},
        "structural_prompt": "Build a linear, two-thread K-drama. A-PLOT: a class divide between the lead (working-class, no leverage, works to survive) and the second lead (an heir inside a chaebol family the user names). B-PLOT ENGINE: a corporate-power struggle — a succession fight or hostile takeover inside that family's empire. Braid them from Chapter 1: the two threads must share ONE device — a CONTRACT (a fake engagement, a binding employment clause, a signed arrangement) that reads as rom-com friction on the surface but is secretly a piece on the succession board. Every act advances BOTH: the couple bicker, negotiate, and thaw (A) while the contract's terms quietly reposition who controls the company (B). Escalate from personal to corporate — boardroom votes, share blocks, a rival faction — never mere bickering.\n\nPlant early, re-readably, that the arrangement was never romantic logistics but a leverage play. At the MIDPOINT, fire the CHAEBOL-SUCCESSION REVEAL: the lead's true place in the family, or the real purpose of the contract, surfaces and recontextualizes every earlier scene — the meet, the terms, who chose whom. Harden the tone here from rom-com to melodrama.\n\nPost-midpoint: fallout — the contract weaponized, the couple split by duty and shame, the empire tightening. Penultimate chapter = B-PLOT CLIMAX: the takeover/succession resolves (won, lost, or exposed) at real corporate cost. Final chapter = A-PLOT lands: an EARNED UNION only after that fallout, chosen freely once the leverage is gone — not restored, rebuilt.\n\nDO NOT write two wounded adults on an unplanned road-trip drifting into love while fleeing their lives; no dying-parent reconciliation, no controlling-mother ultimatum, no ex's engagement announcement, no single-thread mood piece. The contract and the succession war must drive every chapter.\n\nTRUST THE READER: cite no real-world statistics; define no term inline (no glossary asides, no em-dash gloss for cultural or business words); never call the story \"the drama\" or address the reader (\"you already know\"). Be the story, do not narrate it.",
        "chapter_spine": ["Ch 1: Class collision — lead and heir bound by the CONTRACT; succession board SEEDED as backdrop", "Ch 2: Terms enforced — forced proximity thaws the pair (A) while the contract quietly shifts share/vote leverage (B)", "Ch 3: Stakes named — rival faction moves on the empire; the couple's arrangement becomes strategically load-bearing", "Ch ~mid: MIDPOINT TWIST — chaebol-succession reveal recontextualizes the contract; rom-com hardens to melodrama", "Ch pen: B-PLOT CLIMAX — the takeover/succession resolves at corporate cost; contract weaponized, couple split by duty", "Ch final: A-PLOT resolution — earned union rebuilt after the fallout, chosen once the leverage is gone"],
    },
    "revenge_return": {
        "display_name": "Revenge Return",
        "family": "kdrama",
        "tone": "dark_thriller",
        "axes": {"arc": "dual_timeline", "a_conflict": "timing_wronged", "b_plot": "revenge", "twist": "hidden_identity_betrayal", "resolution": "justice_with_cost", "tone": "dark_thriller", "scale": "societal"},
        "structural_prompt": "Build a dual-timeline revenge thriller on a societal scale. Two clocks run at once: PAST — who the lead used to be before the family the user names ruined them (a death, a framing, a home destroyed to protect the institution's rise); PRESENT — that same lead, returned years later under a manufactured identity, embedded inside that family to dismantle it from within. Braid two plotlines in EVERY act.\n\nA-PLOT (relationship): the lead falls, against their own will, for someone bound to the family that wronged them — an heir, a fixer, the one still loyal to the house. Right-person-wrong-time at its cruelest: you cannot love the face attached to the bloodline you came to burn. The wound is timing; the obstacle is the very person tied to the one who wronged the lead.\n\nB-PLOT (engine): a return-to-destroy campaign against a family that is also an institution — the setting the user gives (a conglomerate, a dynasty, a machine of power). Because it props up a whole system, its fall is societal, not private.\n\nEntangle A and B by the setup — cover and heart pull opposite directions. Plant the reveal early, re-readable in hindsight: the false identity, a witness who half-recognizes the lead, an old photograph, a scar.\n\nMIDPOINT: the twist fires — hidden identity surfaces AND a betrayal detonates (the love interest or a trusted ally exposed as complicit in the original crime). Everything before recontextualizes.\n\nPENULTIMATE chapter = B-plot climax: the institution falls, the wrongdoer exposed, justice lands. FINAL chapter = A-plot resolution: justice with a personal cost — the win costs the lead the relationship, or love survives only scorched.\n\nCold, controlled dread throughout; no warmth without menace beneath it. This is NOT a drifting two-hander: no unplanned journey, no slow-burn mood piece, no single thread — the revenge engine must drive every chapter. Cite no real-world statistics or dates; define no term inline; never reference \"the drama,\" the genre, or the reader's expectations.",
        "chapter_spine": ["Ch 1-2 SETUP: present-day return under a false face + past-timeline glimpse of the ruin the family caused (B-plot seed); first collision with the love interest bound to the house", "Ch 3-4 ENTANGLE: the lead works the family from inside while the forbidden pull grows; stakes named as societal — the institution's fall implicates a whole system; reveal-clues planted (photograph/witness/scar)", "Ch ~mid MIDPOINT TWIST: hidden identity surfaces AND a betrayal detonates — the love interest or trusted ally exposed as complicit in the original crime; everything prior recontextualizes", "Ch mid+ FALLOUT: masks drop, the family strikes back, the two timelines converge; trust shatters and the revenge campaign turns lethal and personal", "Ch penultimate B-PLOT CLIMAX: the institution falls — the wrongdoer is exposed and justice lands on a societal scale", "Ch final A-PLOT RESOLUTION: justice-with-a-personal-cost — victory costs the lead the relationship, or love survives only scorched"],
    },
    "birth_secret_makjang": {
        "display_name": "Birth-Secret Makjang",
        "family": "kdrama",
        "tone": "makjang",
        "axes": {"arc": "linear", "a_conflict": "family_class", "b_plot": "family_secret", "twist": "birth_secret_incest_fakeout", "resolution": "rupture_then_reconcile", "tone": "makjang", "scale": "family"},
        "structural_prompt": "Build a linear, escalating family saga where a class-divided romance (A-plot) and a concealed-parentage lie (B-plot) are the same secret seen from two angles — braid them, never run them apart. SLOTS: the lead (raised low-status), the second lead (raised high-status inside the powerful family the user's topic supplies), the matriarch/patriarch guarding the family's standing, the family institution the topic implies. Do NOT write two wounded strangers on an unplanned journey drifting into love while fleeing their lives; ban the dying-parent-for-reconciliation + controlling-mother + ex's-engagement-announcement + big-gesture-reunion skeleton and single-thread mood-over-incident. This is incident-driven and makjang: each post-midpoint chapter detonates a NEW reveal that raises stakes, never repeats a mood.\n\nSETUP: the leads fall despite a class gulf the elders enforce; plant — quietly, re-readably — a mismatch (a birth record, a kept token, a nurse's guilt, a face that echoes the wrong parent) that a first read takes as ordinary. Entangle: every scene of the courtship also tightens the parentage question; the same obstacle keeps them apart AND circles the lie.\n\nMIDPOINT TWIST (fire here, recontextualize everything before it): the birth-secret surfaces and stages the almost-incest fakeout — the two appear to share a parent, so the love reads as forbidden. Play the horror fully.\n\nFALLOUT: rupture. The fakeout is FALSE — a swap/switched lineage means they are NOT blood-related; the real scandal is who was displaced and who profited. Each chapter exposes a new party's complicity; class positions invert as true parentage lands.\n\nResolve in sequence: penultimate = B-PLOT CLIMAX — the lineage exposed publicly, the family reordered, the guilty faced. Final = A-PLOT — the couple, freed of the false taboo and the old class rank, reconcile after the rupture; earned, not gestured.\n\nBans: cite no real-world statistics; define no cultural term inline (weave terms into action, never gloss, never twice); never call this a drama/story or reference the genre — be the story.",
        "chapter_spine": ["Ch 1-2 — SETUP: class-forbidden courtship begins; B-plot SEED planted as an ordinary birth-record/token mismatch", "Ch 3-4 — ENTANGLE: love deepens as the parentage question tightens; elders escalate the class barrier, stakes named", "Ch mid — MIDPOINT TWIST: birth-secret surfaces + almost-incest fakeout fires — the love now reads as forbidden, all prior beats recontextualized", "Ch mid+1 — RUPTURE: fakeout proven FALSE (a swap, not shared blood); they separate anyway as the real displacement scandal cracks open", "Ch mid+2..pen-1 — MAKJANG ESCALATION: each chapter a new reveal — who swapped whom, who profited, class ranks invert", "Ch pen → final — PEN: B-PLOT CLIMAX, true lineage exposed publicly and family reordered; FINAL: A-PLOT reconciliation, couple united past the false taboo and old rank"],
    },
    "fantasy_bond": {
        "display_name": "Fantasy Bond",
        "family": "kdrama",
        "tone": "romantic_fantasy",
        "axes": {"arc": "non_linear", "a_conflict": "fate", "b_plot": "fantasy_fate", "twist": "curse_reincarnation", "resolution": "sacrifice_or_reunion_across_time", "tone": "romantic_fantasy", "scale": "personal"},
        "structural_prompt": "Braid two threads that share one supernatural cause. A-PLOT: the lead and the second lead are pulled together by a fate neither chose — recognition without a first meeting, a bond that should be impossible. B-PLOT (fantasy-fate): one of them is bound to a supernatural office or condition per the user's topic — an immortal, a soul-reaper, someone cursed — governed by hard rules with a running cost: a life for a life, a memory erased each crossing, a countdown of passages left. The B-plot advances by ITS OWN mechanism — a duty performed, a rule broken, the cost accruing — not by whether the couple grows closer.\n\nARC IS NON-LINEAR: open OUT of order — an earlier lifetime, or an ending glimpsed first. PLANT THE TWIST EARLY and re-readably: a wound that predates the meeting, a name the wrong person answers to, an object older than both.\n\nMIDPOINT CHAPTER: fire the reversal — a CURSE or REINCARNATION reveal. They met before across lifetimes, or a curse bound them and one already caused the other's death long ago. Everything prior recontextualizes: the pull was memory, the office was penance, the rules were built around this one soul. Now the cost targets the bond: loving them accelerates the ledger, or resets a memory.\n\nPENULTIMATE CHAPTER = B-PLOT CLIMAX: the debt comes due; office, curse, or ledger forces its price — the mechanism detonates, not the couple. FINAL CHAPTER = A-PLOT RESOLUTION: a SACRIFICE that pays the price so the other lives, OR a REUNION ACROSS TIME (a next life, a broken loop) — earned, not granted. Keep SCALE PERSONAL: one bond, one soul, one price. Tone ROMANTIC-FANTASY — luminous, aching, rule-bound wonder.\n\nDO NOT write two wounded strangers road-tripping into love while fleeing their lives — no dying parent for reconciliation, no controlling mother, no ex's engagement, no duty-versus-career pull, no airport gesture. The obstacle is METAPHYSICAL and RULE-BOUND, not familial. TRUST THE READER: reveal the supernatural rules through what they cost, never a spelled-out glossary or definition; cite no real-world statistics or dates; never wink at the audience or name the genre ('the drama', 'you already know', 'as in every love story'). Let the ledger and the bond carry it.",
        "chapter_spine": ["SETUP: cold-open out of sequence (a death, a parting, an earlier life) + present-day meeting where an impossible pull lands + B-PLOT SEED — the supernatural office/condition and its running cost shown quietly, never explained [A+B]", "ENTANGLE: the pull deepens as the leads circle each other, while the mechanism keeps exacting its cost and the planted twist-objects surface as 'coincidence' [A+B]", "STAKES NAMED: proximity starts bending the rules — the cost begins tilting toward the other lead; both threads point at the same buried thing [A+B, twist primed]", "▶ MIDPOINT TWIST: CURSE / REINCARNATION reveal — they met before across lifetimes, or one already caused the other's death; the pull was memory, the office was penance [recontextualizes everything prior]", "FALLOUT + B-PLOT CLIMAX (penultimate): loving them accelerates the ledger; the debt comes due and the office/curse/ledger forces its price — the mechanism detonates, not the couple", "A-PLOT RESOLUTION (last): a SACRIFICE that pays the price so the other lives, OR a REUNION ACROSS TIME (a next life, a broken loop) — personal scale, earned not granted"],
    },
    "workplace_slow_burn": {
        "display_name": "Workplace Slow-Burn",
        "family": "kdrama",
        "tone": "grounded_melodrama",
        "axes": {"arc": "linear", "a_conflict": "rivalry_to_respect", "b_plot": "medical_legal_procedural", "twist": "villain_true_motive", "resolution": "partnership_both_senses", "tone": "grounded_melodrama", "scale": "institutional"},
        "structural_prompt": "Build a linear workplace drama braiding TWO threads that must both advance in every chapter, never one alone.\n\nA-PLOT (relationship): the lead and the second lead are professional rivals inside the institution the topic names — competing for the same case, byline, or credit, each certain the other's method is wrong. Their arc is rivalry EARNING its way to respect: not attraction at first sight, but grudging proof-of-competence traded case by case until respect turns warmer. Keep the romance under the surface — the slow burn beneath the work, never its subject.\n\nB-PLOT (engine): one live medical, legal, or newsroom case — per the topic's institution — running as a procedure across the whole story: intake, investigation, the filing or broadcast, the verdict or aftermath. It escalates chapter to chapter and touches specific families, so stakes are institutional AND personal, never abstract. Keep it grounded melodrama: real weight, restrained delivery.\n\nSEED EARLY, then COLLIDE: in early chapters plant one buried irregularity and one figure who blocks the leads as \"just procedure.\" At the MIDPOINT, fire the reversal — that figure's true motive surfaces as a scandal, a cover-up, recontextualizing every earlier obstruction and realigning the leads from rivals-against-each-other to partners-against-the-cover-up. It must re-read as planted, not sprung.\n\nRESOLVE IN SEQUENCE: the B-plot climaxes in the PENULTIMATE chapter — the case closes, truth exposed at institutional cost. The A-plot lands LAST: partnership in both senses, chosen out loud — a professional team AND a couple.\n\nDO NOT write two wounded strangers on an unplanned journey falling in love while fleeing their lives. Ban a dying parent for reconciliation, a controlling mother, an ex's engagement, a duty-versus-love pull as the engine, an airport or altar reunion, and mood-over-incident drift. No invented statistics, no case numbers, no fabricated data, no defined jargon or glossary — convey the institution through action and consequence. Never break the frame: no narrator winks, no \"the drama,\" no \"you already know.\"",
        "chapter_spine": ["Rivals assigned the same case — first clash of methods; the case's buried irregularity is quietly planted (B-plot seed).", "Forced to work the case together; grudging respect begins as the stakes reach a specific family, and a gatekeeper stalls them as \"just procedure.\"", "Respect deepens under pressure; the leads circle the irregularity while the gatekeeper's obstruction hardens — the trap set.", "▶ MIDPOINT TWIST: the gatekeeper's true motive breaks open as a scandal/cover-up — every earlier block re-reads as concealment; rivals realign into partners against it.", "B-PLOT CLIMAX (penultimate): fallout and institutional retaliation drive the case to its close — the truth is exposed at real cost, respect now unbreakable.", "A-PLOT RESOLUTION (last): with the work done, they choose partnership in both senses — a professional team and, finally, a couple."],
    },
    "second_lead_triangle": {
        "display_name": "Second-Lead Triangle",
        "family": "kdrama",
        "tone": "melancholic_melodrama",
        "axes": {"arc": "dual_pov", "a_conflict": "timing", "b_plot": "light_or_none", "twist": "second_lead_syndrome", "resolution": "first_lead_honor_second", "tone": "melancholic_melodrama", "scale": "personal"},
        "structural_prompt": "Build a dual-POV melodrama on a personal scale: alternate the lead and the second lead so both desires stay alive. The A-plot is a timing wound — the lead and the first lead are right for each other but keep colliding at the wrong moment (a prior tie, a promise made too early, a beat missed by minutes). The B-plot engine is NOT an institution; it is the second lead's own campaign — a patient, decent pursuit of the lead with its own momentum and a private deadline. Braid every act: each first-lead beat pulling the lead one way is answered by a second-lead beat earning ground the other. In setup, entangle them — the second lead is not a rival dropped in to lose, but someone whose kindness the lead leans on, so choosing hurts. Plant the reversal re-readably: seed moments where the second lead shows up, remembers, waits — a first read files them as \"the friend,\" a re-read as the better love.\n\nAt the midpoint, fire second-lead-syndrome: the lead and the reader realize the \"wrong\" choice is the sympathetic, worthier one — recontextualizing every earlier dismissal. Do NOT resolve it here; let it ache. Post-midpoint, escalate the ache, not the plot: near-misses, an almost-yes, the second lead's hope cresting.\n\nPenultimate = the B-plot climax: the second lead's arc lands FIRST — they step back with dignity or make the sacrifice-choice, honored and whole, never humiliated. Final = the A-plot resolution: the lead chooses the first lead, the beloved harder-won right choice — but bow to the second lead with a true ending, not a discard.\n\nAnti-clone: NOT two wounded strangers falling in love on a road trip while fleeing their lives — no dying parent, no controlling mother, no rival's engagement announcement, no big-gesture reunion, no predictable single mood-thread. The three-way geometry of real interiorities IS the engine; keep all three POVs load-bearing. Never cite a statistic, never define a cultural term, never call this \"the drama.\"",
        "chapter_spine": ["Ch 1-2 — Setup: dual-POV open; the lead and first lead spark but mistime; SEED the second lead's campaign as the one who quietly shows up", "Ch 3-4 — Entangle: first-lead pull vs second-lead ground each act; the lead comes to lean on the second lead's kindness; the timing wound named", "Ch ~mid — MIDPOINT TWIST (second-lead-syndrome): the lead and reader see the 'wrong' choice is the worthier one — every earlier dismissal recontextualized; unresolved, aching", "Ch mid+ — Fallout of the heart: escalate the ache not the plot; near-misses, an almost-yes, the second lead's hope cresting toward its private deadline", "Ch pen — B-PLOT CLIMAX: the second lead's arc lands FIRST — they step back with dignity or make the sacrifice-choice, honored and whole", "Ch final — A-PLOT RESOLUTION: the lead chooses the first lead (the beloved harder-won right choice) — but the ending bows to the second lead, not a discard"],
    },
    "terminal_melodrama": {
        "display_name": "Terminal Melodrama",
        "family": "kdrama",
        "tone": "tearjerker",
        "axes": {"arc": "linear", "a_conflict": "internal_fear_loss", "b_plot": "medical", "twist": "terminal_illness_reveal", "resolution": "un_healed_cherish_time", "tone": "tearjerker", "scale": "personal"},
        "structural_prompt": "Tell a LINEAR two-thread K-drama. STAKES stay PERSONAL — the whole world is two people and one prognosis. TONE: controlled tearjerker melodrama — earn tears through incident, never wring them.\n\nA-PLOT (relationship, INTERNAL conflict = fear of loss): the leads are drawn together, but one enforces distance — declining the shared future, sabotaging closeness whenever it deepens. It reads as commitment-phobia; it is armor against grief. Every act must move this bond while leaving the distance unexplained.\n\nB-PLOT ENGINE (medical — a live hospital machine, not a mood): the distancing lead is secretly managing a terminal illness inside a real clinical apparatus — intercepted results, a hidden pill schedule, a fought-for trial slot, a doctor bound by confidentiality as a third party. This engine must generate INCIDENT: near-exposures, forged normalcy, a countdown, a trial that raises then dashes hope. Every act advances it too.\n\nPlant the twist EARLY and re-readably — an unexplained absence, a strange calm about \"later.\" At the MIDPOINT, concealment collapses: the illness is REVEALED, recontextualizing everything. The distance was never fear of commitment; it was mercy — a withdrawal to spare the other the coming loss.\n\nPost-midpoint, the reveal RUPTURES them — betrayal of the lie, or a flight to spare grief. PENULTIMATE = B-PLOT CLIMAX: the medical question closes — trial verdict lands, last protocol spent, prognosis fixed. NOT cured. FINAL = A-PLOT RESOLUTION, un-healed: they do not beat the illness; they choose the borrowed time. Fear of loss resolves not by escaping loss but by accepting that love with an expiry is still worth it.\n\nDO NOT write: a dying PARENT for reconciliation; a controlling mother; an ex's engagement; a duty-vs-love tug; a road trip of two fleeing adults; a grand airport reunion. Reject mood-over-incident single-thread drift — the hospital thread must carry plot, not atmosphere. NO statistics or survival-rate citations. NO defining medical or cultural terms; weave them into action. Never call this \"the drama\" or \"the story\" — be it.",
        "chapter_spine": ["Ch 1 — Setup: the leads meet and pull close; one quietly enforces distance. B-PLOT SEED: a slipped symptom, a hidden appointment.", "Ch 2 — Entangle: the bond deepens against resistance while the clinical machine activates (results, a trial application); concealment's stakes are named.", "Ch 3 — Rising: a near-exposure and a hope spike (trial slot won) as the distancing lead keeps withdrawing, still unexplained.", "Ch 4 — ▶ MIDPOINT TWIST: concealment collapses — the terminal illness is REVEALED; every act of distance re-reads as mercy.", "Ch 5 — Fallout → B-PLOT CLIMAX (penultimate): rupture, then the medical question closes — trial fails, prognosis fixed, not cured.", "Ch 6 — A-PLOT RESOLUTION (final): un-healed; they stop fleeing the loss and choose the borrowed time together."],
    },
    "class_war_romance": {
        "display_name": "Class-War Romance",
        "family": "kdrama",
        "tone": "social_drama",
        "axes": {"arc": "ensemble_braided", "a_conflict": "family_class", "b_plot": "political", "twist": "villain_motive_corruption", "resolution": "system_costs_couple", "tone": "social_drama", "scale": "societal"},
        "structural_prompt": "Build an ensemble-braided social drama where a romance across a class line and a fight over a corrupt institution advance together until they collide.\n\nA-PLOT (family-class): the lead and second lead sit on opposite sides of a class boundary the setting gives — one born into the privilege the topic names, one shut out by it. The pull is real from the first meeting, but every family treats the match as a threat: one guards its name, the other its pride. The obstacle is not one controlling parent or a duty-versus-love ache — it is a whole social order, enforced by many hands.\n\nB-PLOT (political — class, corruption, privilege): the same institution keeping them apart is quietly rotten — a rigged rule, a bought verdict, a comfort paid for by someone below. Run it through an ENSEMBLE: give two others (a striver, a fixer, a witness) their own stakes, so the class war is society's, not a couple's. Entangle early: the lead's love and the lead's place in the system are the SAME choice by chapter two.\n\nMIDPOINT — the reversal fires here and rewrites everything before it: expose the antagonist's TRUE motive, and through it the corruption at the institution's core — then reveal one lover, or their family, is bound to that rot as beneficiary or buried victim. Plant it early (an off-hand favor, a missing name, a too-clean record), re-readable once known. It turns the class line into a wound and splinters the ensemble.\n\nPENULTIMATE — B-plot climax: the corruption is forced into the open, and the system answers on its terms.\n\nFINAL — A-plot resolution: the system takes something permanent from the couple — a name, a home, a future, a person. They do not defeat it; they choose each other knowing the price.\n\nCONSTRAINTS. Avoid the default engine: no road-trip away from their lives, no rival's engagement announcement, no big-gesture reunion, no dying-parent reconciliation, no clean win — incident over mood, not mood over incident. Trust the reader: cite no real-world statistics or figures; never pause to define a term or gloss the class system inline — let it surface through scene; never name the genre or address the reader (\"the drama,\" \"as you know,\" \"you already know\"). Dramatize; do not annotate.",
        "chapter_spine": ["Ch 1 — Meet across the class line; SEED the institution's quiet rot (A + B both open)", "Ch 2 — Families move against the match; ensemble (striver/fixer/witness) staked to the system; love and standing fused into one choice", "Ch 3 — ▶ MIDPOINT TWIST: antagonist's true motive exposed, corruption at the core revealed, one lover/their family bound to that rot (everything recontextualized)", "Ch 4 — Fallout: the reveal becomes a wound between them; ensemble fractures along the class line; stakes go societal", "Ch 5 — B-PLOT CLIMAX: corruption forced into the open, the system answers on its own terms", "Ch 6 — A-PLOT RESOLUTION: the system exacts a permanent cost; they choose each other knowing the price (no clean win)"],
    },
    "amnesia_reset": {
        "display_name": "Amnesia Reset",
        "family": "kdrama",
        "tone": "melodrama",
        "axes": {"arc": "non_linear", "a_conflict": "internal_identity", "b_plot": "family_secret", "twist": "memory_loss_refalling", "resolution": "love_past_memory", "tone": "melodrama", "scale": "family"},
        "structural_prompt": "Build a NON-LINEAR two-strand K-drama. Open AFTER the rupture: the lead wakes with a hole in memory where the whole relationship used to be, then circle back through out-of-order fragments the reader reassembles. A-PLOT (relationship): the wound is INTERNAL and about IDENTITY — the lead cannot trust a self they no longer remember; the second lead must love someone who no longer knows they were loved. Ask each act: who am I if the self that chose this person is gone? B-PLOT (engine): a FAMILY SECRET braided causally to the amnesia — what was forgotten IS the secret, or the secret caused the injury (hidden parentage, a covered-up accident, a switched child). Entangle, never parallel: recovering a memory exposes the secret; protecting the secret means keeping the lead from remembering. A parent or guardian is quietly complicit — they prefer the amnesia.\n\nMIDPOINT: fire the twist at the middle chapter — the memory-loss is revealed as tied to the buried secret, not a plain accident, AND the re-falling ignites at once: the blank-slate lead falls for the second lead a SECOND time. Recontextualize earlier fragments so Ch1 re-reads differently. Plant it early; no cheap surprise.\n\nFALLOUT: the family closes ranks; remembering now threatens the family, not just the couple. Escalate melodrama at FAMILY scale — kin-sized and airless, no corporate war, no citywide stakes.\n\nPENULTIMATE = B-PLOT CLIMAX: the secret is exposed; the guardian's complicity breaks open. FINAL = A-PLOT RESOLUTION: love PERSISTS PAST MEMORY — the bond re-forms whether or not memory returns; the choosing, not the remembering, holds.\n\nUse SLOTS only (lead, second lead, family/guardian, given setting). Steer AWAY from the road-trip skeleton: no strangers fleeing on an unplanned journey, no dying-parent reconciliation, no controlling-mother-plus-ex's-engagement, no airport reunion. TRUST THE READER: dramatize through scene and fragment — never cite real-world statistics, never gloss a term inline, never break frame with genre self-reference (\"the drama,\" \"you already know\").",
        "chapter_spine": ["Ch1 — Open post-rupture: the lead wakes memory-blank; a stranger (the second lead) insists on a shared past; family-secret SEEDED in a fragment that reads as innocent", "Ch2 — Out-of-order 'before' fragments accrue; the couple's prior bond and the guardian's odd vigilance both surface; stakes named — remembering endangers more than the heart", "Ch3 — Entangle: chasing one recovered memory brushes the secret; the guardian steers the lead away from remembering; the second lead is torn between honesty and protecting them", "Ch4 — MIDPOINT TWIST: the amnesia is exposed as bound to the buried family secret (not a plain accident) AND the re-fall ignites — blank-slate lead falls a SECOND time; earlier fragments recontextualized, Ch1 re-reads differently", "Ch5 — B-PLOT CLIMAX (penultimate): the family closes ranks, then the secret detonates; the guardian's complicity breaks open; the couple ruptures under the exposed truth", "Ch6 — A-PLOT RESOLUTION (final): love PERSISTS PAST MEMORY — whether or not the memories return, the lead chooses the second lead anew; the choosing, not the remembering, is what holds"],
    },
    "timeslip_fate": {
        "display_name": "Time-Slip Fate",
        "family": "kdrama",
        "tone": "romantic_fantasy",
        "axes": {"arc": "dual_timeline", "a_conflict": "timing_across_eras", "b_plot": "fantasy_fate", "twist": "time_slip_consequence", "resolution": "alter_fate_or_accept", "tone": "romantic_fantasy", "scale": "personal"},
        "structural_prompt": "Build a DUAL-TIMELINE romantic-fantasy on a PERSONAL scale. Two eras run in parallel; the lead crosses between them through a specific, rule-bound slip mechanism (the exact trigger, threshold, and cost are set by the topic the user gives). BRAID two threads through every chapter. A-PLOT: the lead and the second lead keep loving each other in the WRONG era — one is always older, already-committed, not-yet-met, or a memory to the other. The obstacle is TIMING ACROSS ERAS, never forgetting and never a curse. B-PLOT ENGINE: the time-slip mechanic itself — its rules, its price on the body/memory/the world, and its worsening instability. Treat the mechanism as a system with logic and consequences; the lead learns and TESTS its rules like a puzzle. Reveal the rules only by dramatizing them — a slip that costs, a threshold that fails — NEVER by explaining or defining them to the reader. ENTANGLE early: the first slip must be caused by, and pay off in, the relationship. SEED the twist in Ch 1–2 as a small, re-readable detail (a changed object, a name that shouldn't exist, a scar, a date that won't line up).\n\nMIDPOINT TWIST — a time-slip CONSEQUENCE that REWRITES THE PRESENT. At the midpoint, the lead returns from a slip to find the present altered by something they did across eras: a person now alive or gone, a relationship that no longer happened, a self they no longer are. Everything before re-reads. After: each further slip risks unmaking the very bond they crossed to protect; the two pull further out of sync.\n\nRESOLUTION IN SEQUENCE. Penultimate = B-PLOT CLIMAX: the mechanism forces one last crossing that rewrites fate or seals it, at a named cost. Final = A-PLOT LANDING: they ALTER fate (reunite on the mechanism's terms) or ACCEPT it (release across time). Keep it personal — wonder and ache, not thriller.\n\nBANS: no real-world statistics, dates-as-facts, or cited figures; no glossary, footnote, or inline definition of the mechanic — show it working, don't gloss it; never reference \"the drama\"/\"the story\" or address the reader (\"you already know\"). DO NOT write two wounded adults on a road trip slowly thawing; no dying parent for reconciliation, no controlling mother, no ex's engagement announcement, no duty-vs-love speech, no airport grand gesture. Single-thread mood-piece = fail.",
        "chapter_spine": ["Ch 1-2 SETUP: the two meet across a seam between eras; first rule-bound slip, caused by reaching for the other — and one seeded anomaly (changed object / impossible date), shown not explained", "Ch 3-4 ENTANGLE: they learn the mechanism's rules together by testing it; stakes named — every crossing costs, and they keep loving each other in the wrong era", "Ch ~mid ▶ MIDPOINT TWIST: the lead returns from a slip to a REWRITTEN PRESENT — a slip-consequence has altered who is alive / what happened / who they are; everything prior re-reads", "Ch mid+ FALLOUT: the timelines desync and destabilize; each further slip now threatens to unmake the bond they crossed to save", "Ch pen B-PLOT CLIMAX: the mechanism forces its final crossing — rewrite fate or seal it, at a named, irreversible cost", "Ch final A-PLOT LANDING: they ALTER fate (reunite on the mechanism's terms) or ACCEPT it (release across time) — personal, romantic-fantasy close"],
    },
    "ensemble_family_saga": {
        "display_name": "Ensemble Family Saga",
        "family": "kdrama",
        "tone": "warm_weekend_melo",
        "axes": {"arc": "ensemble_braided", "a_conflict": "internal_family", "b_plot": "family_secret", "twist": "distributed_reveals", "resolution": "family_reconvenes", "tone": "warm_weekend_melo", "scale": "family"},
        "structural_prompt": "Build a warm weekend-drama at family scale around ONE family the user names — not one couple. Cast an ensemble of 3-5 kin (across two or three generations) sharing a home, table, or business; give each their own thread. A-PLOT: the relationship in strain is INTERNAL across this family — estrangement, a favored/overlooked sibling, a parent and grown child who cannot speak plainly, in-laws who never thawed. Advance these bonds every chapter through ordinary domestic ritual (a shared meal, an anniversary, a move, an illness scare), not romance-as-engine. B-PLOT: a single family-secret buried a generation ago, whose consequences surface differently in each thread. Braid tightly: every chapter must nudge BOTH a specific relationship AND leak one more fragment of the buried truth, so the two read as one fabric. Plant the secret early in mundane detail — a name avoided, a locked drawer, a birthday that doesn't add up, an heirloom that shouldn't exist — so a re-reader sees it seeded.\n\nMIDPOINT TWIST — distributed reveals: at the middle chapter, each thread's private secret surfaces AT ONCE, and they resolve into facets of the ONE generational secret. What looked like separate private shames recontextualize as a single concealed act and its long shadow. Everything prior re-reads.\n\nFALLOUT: the family fractures — sides taken, a walkout, silence at the table, an escalation per thread. B-PLOT CLIMAX (penultimate): the full origin of the secret is spoken aloud — who did what, why, and who paid — settling the generational debt. A-PLOT RESOLUTION (final): the family RECONVENES — a reunion around the same table/ritual from chapter one, bonds re-set on honest terms; warm, earned, not saccharine.\n\nSLOTS ONLY: the family, the setting, the era, the secret's nature all come from the user's topic — never invent names, a specific city, company, or event.\n\nDO NOT write the default K-drama skeleton: no two wounded strangers on an unplanned road-trip drifting into love while fleeing their lives; no single-couple spine with a dying parent + controlling mother + an ex's engagement announcement + duty-vs-love pull + one big-gesture reunion. This is a MULTI-THREAD household lattice, not a romance with relatives attached. No statistics, no term definitions, no calling this a \"drama\" or \"story.\"",
        "chapter_spine": ["Ch1-2 — SETUP: the family gathers at the shared table/ritual; each thread's strain sketched; plant the buried secret in mundane detail (avoided name, drawer, mismatched date)", "Ch3-4 — ENTANGLE: threads press against each other; each member privately brushes their own concealment; the generational secret's pressure named without being seen", "Ch~mid — MIDPOINT TWIST (distributed reveals): every thread's private secret surfaces at once and resolves into facets of ONE generational secret — all prior chapters recontextualized", "Ch mid+ — FALLOUT: the family fractures; sides taken, a walkout, silence at the table; each thread escalates under the exposed truth", "Ch pen — B-PLOT CLIMAX: the secret's full origin is spoken aloud — who did what, why, who paid — the generational debt settled", "Ch final — A-PLOT RESOLUTION (family reconvenes): reunion at the same table/ritual from Ch1, bonds re-set on honest terms — warm, earned"],
    },
    "corporate_thriller": {
        "display_name": "Corporate Thriller",
        "family": "kdrama",
        "tone": "taut_dark_thriller",
        "axes": {"arc": "linear", "a_conflict": "ambition_clash", "b_plot": "corporate_power", "twist": "villain_motive_betrayal", "resolution": "win_company_lose_something", "tone": "taut_dark_thriller", "scale": "corporate"},
        "structural_prompt": "Build a taut corporate thriller in strict linear time. A-plot: two ambitious equals — the lead and the second lead — want the same summit and cannot both have it. Braid it act by act with a B-plot corporate-power engine (a hostile takeover, or a whistleblower dossier that could gut the firm) inside the institution the topic names. Do NOT write the default K-drama: no aimless road trip, no two wounded strangers thawing while they flee their lives, no dying-parent reconciliation, no controlling-mother veto, no ex's engagement announcement, no duty-versus-love ache resolved by an airport gesture. Mood-over-incident and single-thread are failure here; every chapter moves ONE concrete plot lever AND one relationship beat.\n\nSETUP: the two meet as rivals-or-allies chasing the same win; plant the takeover/dossier as ambient background — a board memo, a leaked figure, a routine-looking offer. Bury the villain's true motive in plain sight so a re-read pays off.\n\nENTANGLE: their ambitions and the corporate war fuse — shared strategy, a co-authored move, mutual leverage. Name stakes in corporate terms: the chairmanship, the ledger, the exposure.\n\nMIDPOINT TWIST — fire it exactly at the middle chapter: reveal the villain's TRUE motive AND a betrayal that recontextualizes every earlier scene (the ally who briefed the lead was steering the takeover; the mentor's grief masked a grab; the trusted one planted the seed). The setup must now read differently.\n\nFALLOUT: alliance breaks, positions invert, the war turns personal and colder.\n\nPENULTIMATE = B-PLOT CLIMAX: the takeover closes or the dossier detonates; the company is won.\n\nFINAL = A-PLOT LANDING: winning the company costs something irreversible — the second lead, a principle, or the self who could still be loved. Win the empire, lose the person. No clean triumph, no reconciliation hug. Stay topic-agnostic: use the firm, the family, the setting the user gives. Never cite a statistic. Never define a term. Never call this a story.",
        "chapter_spine": ["Ch 1-2 SETUP: two ambitious equals collide over the same summit; the takeover/dossier seeds as routine background (villain's true motive hidden in plain sight)", "Ch 3-4 ENTANGLE: rivalry sharpens into uneasy alliance; ambitions and the corporate war fuse; stakes named — chairmanship, ledger, exposure", "Ch ~mid ▶ MIDPOINT TWIST: villain's TRUE motive revealed + the betrayal — the trusted ally was steering the takeover all along; every earlier scene recontextualized", "Ch mid+ FALLOUT: alliance shatters, positions invert, leverage becomes weapon; the fight turns personal and colder", "Ch pen B-PLOT CLIMAX: the takeover closes / the dossier detonates — the company is won", "Ch final A-PLOT LANDING: victory exacts an irreversible cost — the second lead, a principle, or the self that could be loved; win the empire, lose the person"],
    },
    "light_comedic": {
        "display_name": "Light Comedic",
        "family": "romance",
        "tone": "warm_comedic",
        "axes": {"arc": "linear", "conflict": "misunderstanding_that_fizzles", "resolution": "earned_warm", "pov": "first", "twist": "none"},
        "structural_prompt": "Tulis cerita romansa ringan-komedik dalam POV pertama (\"aku\") yang observasional dan sedikit sarkastis pada diri sendiri — BUKAN kontemplatif, BUKAN penuh tesis batin. Suara \"aku\" harus punya timing komedi: kalimat pendek yang menjatuhkan diri sendiri, jeda yang dipakai untuk lelucon kecil, bukan untuk menghela napas.\n\nARC LINEAR: waktu berjalan maju satu arah. Tidak ada flashback, tidak ada mimpi, tidak ada bingkai \"ia mengenang\". Satu hari, atau rentang pendek berurutan, dari salah-baca → salah-paham puncak → salah-paham kempis.\n\nKONFLIK: misunderstanding_that_fizzles. \"Aku\" menyimpulkan sesuatu tentang orang itu (atau perasaan orang itu ke aku) dari data yang terlalu sedikit. Konflik ini murni internal-persepsi, BUKAN cinta segitiga, BUKAN kecemburuan, BUKAN campur tangan orang tua. Salah paham naik bertingkat karena aku terus menafsir baru lewat lensa yang keliru. Puncaknya bukan pertengkaran — puncaknya adalah momen canggung yang, dari luar, terlihat konyol.\n\nCARA MENGEMPIS: bukan pengakuan besar. Satu kalimat sehari-hari dari orang itu — permintaan biasa, komentar sepele, koreksi kecil — yang membuat seluruh premis asumsiku runtuh dalam satu detik. Ledakan tawa (atau ledakan malu yang jadi tawa) menggantikan grand-gesture confession.\n\nLANDING: earned_warm. Bahagia tanpa ragu, tapi bukan lewat pelukan sinematik atau deklarasi. Dinyatakan lewat gestur kecil yang menandakan mereka akan lanjut: berbagi bangku, satu tawaran remeh diterima, rencana kecil untuk besok. Hangat, ringan, tidak menggurui, tidak manis berlebihan.\n\nTWIST: none. Tidak ada belokan identitas, tidak ada rahasia tersembunyi. Yang mengejutkan hanya seberapa sederhana kebenarannya dibanding bangunan asumsi \"aku\".\n\nTRUST THE READER: jangan kutip statistik, jangan definisikan istilah, jangan self-reference genre. Tunjukkan situasi, biarkan pembaca ikut menyimpulkan.",
        "chapter_spine": ["Bab 1 — Aku salah baca situasi kecil hari ini, dan aku belum tahu itu akan jadi lucu", "Bab 2 — Salah bacaku menempel: aku bertindak seolah asumsiku benar, dan sikapku jadi aneh dengan cara yang bisa ditertawakan", "Bab 3 — Asumsiku bertemu bukti kecil yang seharusnya membatalkannya; aku memilih menafsirkannya sebagai konfirmasi", "Bab 4 — Salah paham naik ke titik canggung tertinggi — bukan lewat air mata, tapi lewat momen yang kalau diceritakan besok pasti bikin malu sendiri", "Bab 5 — Satu kalimat biasa dari orang itu mengempiskan seluruh bangunan asumsiku; aku ketawa duluan sebelum sempat malu", "Bab 6 — Kami melanjutkan hari dengan lebih ringan; sesuatu yang hangat tetap tinggal, dinyatakan lewat gestur kecil, bukan pidato"],
    },
    "we_vs_world_close_third": {
        "display_name": "We-vs-World (Close-Third)",
        "family": "romance",
        "tone": "grounded_serious",
        "axes": {"arc": "linear", "conflict": "external_sympathetic_force", "resolution": "earned_costly", "pov": "close_third", "twist": "none"},
        "structural_prompt": "Write in CLOSE THIRD PERSON throughout — \"Ia\" or the character's name, never \"aku\", never \"kamu\"-as-narrator. The camera sits behind one shoulder; we hear one interior voice at a time, but the pronoun stays third. Switch shoulder at most once, at a clear scene break.\n\nThe bond forms early and is NEVER in doubt. Two people recognize each other cleanly — no misread signals, no jealousy, no third party. What they feel is real from the first real scene. The story is not \"will they\" — it is \"how do they carry this.\"\n\nThe OBSTACLE is a legitimate, sympathetic EXTERNAL force. Name it concretely: a duty owed to family, a caregiving obligation, a signed contract, a geography that will not bend, a promise made before they met. It has its own dignity. The person or institution embodying it is not cruel, not scheming, not a villain to be defeated — they have reasons a fair reader would nod at. The story must honor both sides of the obstacle: the couple's want AND why the force exists.\n\nOPENS: a quiet scene where the two are already inside each other's orbit; the obstacle is present but unnamed — a phone left face-down, a calendar, a suitcase that hasn't been unpacked. Establish tenderness without declaration.\n\nTURNS: the obstacle becomes concrete and unmovable in ONE specific scene — a date, a document, a person on the other end of a call. No one is wrong. Both characters see it at the same time. This is the axis the story pivots on, not a misunderstanding.\n\nLANDS: earned and costly. They choose each other, but something real is paid — a delay measured in seasons not chapters, a role given up, a distance kept for a named reason. The final image shows the cost AND the choosing, held together. Warm but not clean. No grand-gesture speech, no airport chase, no rain-kiss.\n\nFORBIDDEN: shared earphones in rain, hair \"diikat asal\", orbit/gravity metaphors, mom calling about grades, re-reading chats like poetry, \"keberanian bukan absennya rasa takut\", jealousy beats, misunderstandings, villains, first-person \"aku\" narration.",
        "chapter_spine": ["Bab 1 — Sudah di orbit yang sama: buka pada satu adegan tenang di mana keduanya sudah akrab; sesuatu di latar (kalender, koper belum dibongkar, telepon tengkurap) diam-diam menandai halangan yang belum disebut namanya.", "Bab 2 — Nama untuk yang dirasakan: sebuah momen kecil dan jujur di mana keduanya, tanpa pengakuan besar, sama-sama tahu ini nyata; kamera close-third menempel pada satu bahu, suara batin ketiga tunggal.", "Bab 3 — Bentuk halangan itu: kewajiban eksternal muncul konkret — surat, jadwal, orang di ujung telepon yang punya alasan yang adil; ditulis dengan hormat, bukan sebagai antagonis.", "Bab 4 — Menimbang dengan jujur: keduanya duduk dengan kenyataan itu tanpa saling salahkan; percakapan yang pelan, dewasa, tanpa air mata dramatis; pergeseran bahu kamera boleh terjadi di sini.", "Bab 5 — Yang dibayar: sebuah keputusan yang memakan sesuatu yang nyata — peran, musim, jarak yang diberi nama; halangan tidak dikalahkan, dihormati dan dibawa serta.", "Bab 6 — Gambar terakhir: momen tenang setelahnya; ongkos itu masih terlihat, tetapi keduanya ada di dalam bingkai yang sama; hangat, matang, tidak manis berlebihan."],
    },
    "healing_slice": {
        "display_name": "Healing Slice",
        "family": "kdrama",
        "tone": "warm_slice_of_life",
        "axes": {"arc": "linear", "a_conflict": "gentle_wounds", "b_plot": "craft_or_hearth", "twist": "none", "resolution": "quiet_healing", "pov": "ensemble_close", "scale": "family_hearth"},
        "structural_prompt": "Write a healing slice-of-life in the K-drama register. Two adults carry small, ordinary wounds — a father lately gone, a marriage that quietly ended, a career that thinned into nothing, a friendship that drifted. Neither wound is a secret bomb. Neither person is hiding a twin, an inheritance, or a betrayal. They meet at a hearth — a neighborhood bakery, a used-book shop, a soup kitchen that opens at dawn, a one-room clinic, a repair bench for old radios — and the craft of the place gathers them. The B-plot is the hearth itself: dough that must be started the night before, shelves reshelved, a kettle that whistles when the second person arrives. Everything the A-plot needs, the hearth teaches through small daily gestures.\n\nOpen on a morning routine at the hearth — one person alone, doing the small work well, the wound visible only in what they do not say. The other arrives as a customer, a neighbor, a temporary hand. They come back the next day. And the next. There is no meet-cute crash, no argument-to-love, no memory loss. Just weather, hands, tea, a chair pulled slightly closer.\n\nTurn on a shared small task — a wedding cake for someone else's grandmother, a lost cat in the alley, a power cut that means candles in the shop — that lets each person be tender toward the other without naming it. Nothing shatters. Nothing is revealed. The turn is that they let themselves be seen while doing something ordinary together.\n\nLand quietly and unambiguously healed. The hearth stays open. Both people are lighter, not fixed. A shared plan for the very next day exists — dough to start tonight, a shelf to finish tomorrow. Warmth without sweetness syrup: specific hands, specific weather, specific small food. No wedding, no confession-monologue, no grand reveal, no reunion at an airport. Just: the light turns on earlier now because two people open the shop.\n\nPOV is ensemble-close: rotate freely among the two leads and two or three regulars of the hearth (an elderly patron, an apprentice, a neighbor), each in warm close third. Indonesian narration uses \"ia\" and names, never \"aku\".",
        "chapter_spine": ["Morning at the hearth — one person alone, working well, wound shown only in small omissions and the specific weather of the day", "The other arrives — a small errand at the hearth becomes a second visit, then a third, without either naming a reason", "Hands learn each other's rhythm — a regular of the hearth (elder patron, apprentice, neighbor) is drawn in; the craft carries what talking cannot", "A shared small task lands in their laps — a cake for someone else's celebration, a lost animal, a power cut — and each is tender toward the other in passing", "A quiet evening after the task — tea, leftover bread, a chair pulled closer; each says one true small thing about the wound; nothing is fixed, only witnessed", "Next morning at the hearth — the light is on earlier because two people open it; a plan for tonight's dough or tomorrow's shelf; the door swings and a regular walks in smiling"],
    },
    "uplifting_underdog": {
        "display_name": "Uplifting Underdog",
        "family": "kdrama",
        "tone": "triumphant",
        "axes": {"arc": "linear", "a_conflict": "shared_belief", "b_plot": "systemic_underdog_fight", "twist": "ally_true_capacity", "resolution": "unambiguous_win", "pov": "first_or_close_third", "scale": "community_to_societal"},
        "structural_prompt": "Tulis kisah pemenang bawah-angin yang berakhir MENANG BERSIH. Bukan pahit, bukan mahal, bukan tragedi berbaju harapan. Landing = kemenangan yang tak bisa disangkal, dan pembaca ditinggalkan tegak, bukan pilu.\n\nOPENS: satu orang biasa — bukan pahlawan, bukan jenius — menabrak ketidakadilan sistemik yang dianggap semua orang sebagai \"ya sudah, memang begitu\": peraturan yang menekan, praktik yang membusuk pelan, pintu yang tertutup untuk orang seperti dia. Ia menolak menerimanya. Bukan karena marah. Karena ia melihat sesuatu yang belum dilihat orang lain, dan keyakinan itu tidak bisa ia matikan.\n\nTURNS: perlawanan tumbuh dari satu suara menjadi banyak. A-plot = kemitraan (bisa cinta yang matang perlahan, bisa persaudaraan tempaan) yang ditempa DALAM perjuangan, bukan mengganggu perjuangan — dua orang yang menemukan bahwa keyakinan mereka sama sebelum menemukan bahwa hati mereka sama. B-plot = pertarungan sistemik itu sendiri: bertambah sekutu, bertambah taruhan, sistem mulai membalas. Sekutu tampak biasa hingga TWIST: seorang sekutu ternyata membawa kapasitas tersembunyi — akses, pengetahuan, sejarah, atau kedudukan — yang mengubah geometri pertarungan. Bukan penyelamat dari langit; sesuatu yang selama ini ada di dalam gerakan, tak terbaca.\n\nLANDS: kemenangan bersih dan publik. Sistem bergeser secara nyata — aturan berubah, pintu terbuka, sesuatu yang dulu mustahil menjadi biasa. Pasangan berdiri bersebelahan di sisi lain, utuh. Tidak ada pengorbanan tersembunyi, tidak ada \"tapi\", tidak ada satu tokoh yang harus mati atau mundur agar kemenangan terasa \"layak\". Kemenangan dibayar dengan kerja, bukan dengan kehilangan.\n\nPOV: orang pertama (\"aku\") ATAU orang ketiga dekat pada protagonis bawah-angin — pikiran satu orang, jarak nol. Skala bergerak dari satu komunitas ke lapisan masyarakat yang lebih luas: yang lokal menjadi preseden.\n\nNada: triumfan yang berakar — bukan sorak-sorai, tapi kelegaan yang jujur seorang manusia biasa yang benar dan menang.",
        "chapter_spine": ["Ketidakadilan yang dianggap wajar menabrak satu orang biasa, dan ia menolak menerimanya", "Suara tunggal menemukan suara kedua — kemitraan lahir dari keyakinan yang sama sebelum lahir dari perasaan", "Gerakan tumbuh dari ruang tamu menjadi sesuatu yang tidak bisa diabaikan lagi", "Sistem membalas: tekanan nyata, kehilangan sementara, gerakan hampir retak", "Seorang sekutu membuka kapasitas tersembunyi yang mengubah geometri pertarungan", "Kemenangan publik yang bersih — sistem bergeser, pasangan berdiri utuh di sisi lain"],
    },
}

# ── TWIST_SLOTS (v2, authored+verified workflow) ────────────────────────
TWIST_SLOTS = {
    "none": {
        "display_name": "None",
        "description": "No twist is stacked on this outline. Run the base beat-map exactly as written — its own plants, turns, and payoffs carry the whole arc.",
        "twist_fragment": "No twist is stacked on this outline. Run the base beat-map exactly as written — its own plants, turns, and payoffs carry the whole arc. Do not invent a hidden identity, secret illness, engineered setup, buried past, parallel relationship, unreliable frame, or time jump on top. The midpoint is whatever the beat-map already schedules there; the ending resolves on the base structure's own terms. Trust the preset. Let character interiority, small textured beats, and the specific relational pressure the beat-map already defines do the work. Any surprise in the piece should emerge from behavior and consequence inside the existing arc, not from a layered reveal added here.",
        "plant_early_marker": "No twist to plant — run the beat-map's opening as written.",
        "fire_at_midpoint_marker": "No twist fires — midpoint is the beat-map's own turn, played straight.",
        "incompatible_with": [],
    },
    "third_party_engineered": {
        "display_name": "Third-Party Engineered",
        "description": "A hidden orchestrator engineered the couple's misunderstanding; midpoint exposes the puppetry, and reconciliation requires naming and refusing them.",
        "twist_fragment": "Layer a hidden orchestrator across the base arc without altering its beats. Early on, when the central wound opens, keep a third party physically or digitally present in the periphery: forwarding a message, \"translating\" a comment, arriving with concerned advice, or being the last person one lead spoke to before the misunderstanding calcified. Let their small kindnesses read as loyalty on first pass. At the pivot, surface one concrete artifact — a timestamp, a duplicated screenshot, a witness who misremembers on purpose — that reveals the misunderstanding was engineered. The couple's earlier cruelty now reads as puppetry; their reconciliation must include naming, and refusing, the orchestrator.",
        "plant_early_marker": "In an early scene where the couple first hurts each other, place a silent third figure at the edge of the frame (a colleague copied on an email, a \"helpful\" relative, a smiling rival) whose small action nudges the wound.",
        "fire_at_midpoint_marker": "At the midpoint, expose the orchestrator through a stray receipt/message/witness — one earlier \"damning\" moment now reads as a staged setup, and the couple's fight retroactively becomes the third party's win.",
        "incompatible_with": ["birth_secret_makjang"],
    },
    "one_is_leaving_ill": {
        "display_name": "One Is Leaving / Ill",
        "description": "A hidden-countdown twist where one lead is secretly leaving or ill; plants three concrete tells in the first third and fires at midpoint via a wordless physical artifact that retro-colors every prior warmth as farewell rehearsal.",
        "twist_fragment": "Layer a hidden countdown beneath the base arc: one lead has already decided to leave, or has been given a diagnosis, before the story opens. Do not rewrite the beats — instead, thread three small tells across the first third: a hesitation at thresholds, an odd tenderness about ordinary objects, a refusal to commit to anything far ahead. The other lead notices but files it under \"moodiness.\" At the midpoint, an artifact surfaces the truth without dialogue. From that point, every earlier warmth reads as farewell rehearsal, and the remaining beats must carry the double weight of what they are and what they were secretly for.",
        "plant_early_marker": "In an early scene, show ONE small off-beat gesture from the leaver — lingering a half-second too long at a doorway, photographing something ordinary, declining to make a plan more than two weeks out — witnessed but unremarked-on by the other lead.",
        "fire_at_midpoint_marker": "At the midpoint, force the reveal through a physical artifact the other lead stumbles on (boarding pass, MRI printout, farewell letter draft, packed box) — no monologue explanation; let the object do the work so the reader mentally rewinds the earlier \"off\" moments and re-reads them as goodbye.",
        "incompatible_with": ["secret_reveal", "terminal_melodrama", "amnesia_reset"],
    },
    "parallel_relationship": {
        "display_name": "Parallel Relationship",
        "description": "Parallel Relationship twist: one partner carries an unclosed prior bond the other doesn't know about; planted via three re-readable frictions in Act 1, fired at midpoint when the bond intrudes in person or evidence and retroactively colors the earlier warmth.",
        "twist_fragment": "Layer over the base arc: give one of the two an unclosed parallel bond — an ex they never fully released, a still-living first love waiting somewhere, or a paper-only obligation-marriage abroad — that the other does not know exists. Do not alter the outlined beats; instead thread three quiet frictions through the first third (a phone kept face-down, a birthday remembered with suspicious precision, a hometown they will not name) that read as privacy the first time and as tether the second time. At the midpoint, let the parallel bond intrude in person or in evidence, so the earlier warmth is retroactively colored by what one of them was already holding.",
        "plant_early_marker": "In the first third, plant three small ordinary-seeming frictions around one character — a phone screen flipped face-down, a birthday remembered too specifically, a hometown they refuse to name — each written to read as endearing privacy on first pass and as an unclosed prior bond on re-read.",
        "fire_at_midpoint_marker": "At the midpoint, force a moment where the other party literally encounters the parallel bond — a phone call answered aloud, a stranger who calls them by a claimed name, a document with two signatures — so every earlier hesitation, unreturned call, or evasive \"family thing\" instantly rereads as this hidden tether rather than as ordinary busyness or shyness.",
        "incompatible_with": ["birth_secret_makjang", "secret_reveal"],
    },
    "time_skip_reveal": {
        "display_name": "Time Skip Reveal",
        "description": "Layer a time-skip reveal over the base arc without replacing any beat. In the first third, plant one small tense-slippage the reader can skim: a \"would later\" aside, a photograph held in past-perfect, an object flagged as \"the last time,\" a season half-noticed. Keep the scenes themselves fully present and specific — the base arc still runs. At the midpoint beat, open a paragraph with a hard temporal cue (a wrong season, a new address, an aged detail on the POV character) and let the next sentence reveal these scenes are being remembered from months or years on. Do not restage what we already saw. Let the distance itself change what the earlier tenderness or damage meant.",
        "twist_fragment": "Layer a time-skip reveal over the base arc without replacing any beat. In the first third, plant one small tense-slippage the reader can skim: a \"would later\" aside, a photograph held in past-perfect, an object flagged as \"the last time,\" a season half-noticed. Keep the scenes themselves fully present and specific — the base arc still runs. At the midpoint beat, open a paragraph with a hard temporal cue (a wrong season, a new address, an aged detail on the POV character) and let the next sentence reveal these scenes are being remembered from months or years on. Do not restage what we already saw. Let the distance itself change what the earlier tenderness or damage meant.",
        "plant_early_marker": "In the first third, drop one small tense-slippage or memory-verb the reader will skim past — a \"she would later\" aside, a photograph described in an odd past-perfect, an object noted as \"the last time\" — so the reveal earns a re-read.",
        "fire_at_midpoint_marker": "At the midpoint beat, open a paragraph with a hard temporal cue — a season that shouldn't be here yet, a new address, an aged detail on the POV character's body or possessions — and let the next line reveal the prior scenes were being remembered from months or years later; do not restage, let the reader feel the gap.",
        "incompatible_with": ["breakup_first", "the_almost", "timeslip_fate"],
    },
    "unreliable_narrator": {
        "display_name": "Unreliable Narrator",
        "description": "PASS — authored fragment is additive overlay, plant is concrete/re-readable, fire recontextualizes at midpoint, incompatibilities cover known duplications, no trust-the-reader violations.",
        "twist_fragment": "Overlay only — do not alter the base arc. Anchor the story inside one POV whose voice quietly shapes what the reader sees: from chapter one, have that narrator describe a specific recurring beat (a fight, a favor, a goodbye) with slightly self-flattering phrasing and one small omitted detail that a careful reader could later name. Around the midpoint, let an outside source — another character's account, a message thread, a re-surfaced memory — contradict the narrator on that exact point. The reveal is not a new plot event; it is the narrator conceding, on-page, what was softened. Prior scenes recontextualize themselves through the reader's memory of the loaded phrasing.",
        "plant_early_marker": "In the first two chapters, let the POV narrator describe one recurring event/person using slightly loaded, self-justifying phrasing (a diminutive, an excuse, a passive verb where an active one belongs) and skip past one small factual detail the reader will later be able to point to.",
        "fire_at_midpoint_marker": "At the midpoint, a third party, a document, or a forgotten memory contradicts the POV account on one concrete point — forcing the narrator to admit (to themselves, then on-page) what they left out, softened, or reframed; every prior scene that used the loaded phrasing now reads differently without needing to be re-narrated.",
        "incompatible_with": ["dual_pov_parallel", "amnesia_reset", "birth_secret_makjang", "revenge_return"],
    },
}

# ── AXIS_FRAGMENTS (romance recombination, v2) ──────────────────────────
AXIS_FRAGMENTS = {
    "arc": {
        "linear": "Open on the lead in the setting the user gives, at the ordinary moment before everything shifts. Move forward in strict clock-time: each chapter is the next day, week, or season, no jumps back. The middle is a staircase of consequences where earlier choices bind later ones. Close on the lead standing in the same setting, changed, with the reader having watched every step that brought them there.",
        "non_linear_flashback": "Open on the lead mid-crisis in the setting the user gives, with no explanation of how they arrived. The present-tense spine moves forward across days, but each chapter is punctured by one italicized memory of the person they love, delivered out of order. The middle assembles the past like scattered photographs. Close when the final memory lands and the reader finally understands what the opening image cost.",
        "dual_timeline": "Alternate two clearly labeled timelines: THEN, when the lead first met the person they love in the setting the user gives, and NOW, years later, when something has gone wrong. Chapters trade off strictly. The middle lets each timeline answer questions the other raises, tightening as they converge on a single date. Close when both timelines arrive at the same room, the same hour, and the gap between them collapses.",
        "dual_pov": "Open with the lead's chapter in the setting the user gives; the second chapter is the person they love, same day, different room, different voice. The book alternates strictly, each voice withholding what the other assumes. The middle is a slow correction of misreadings the reader can see and neither character can. Close on one final chapter where the two voices finally address the same moment and disagree about what it meant.",
        "retrospective_first": "Open at the ending: the lead, older, in the setting the user gives, telling the reader plainly how it turned out with the person they love. Then step back to the beginning and walk forward, the retrospective voice occasionally interrupting to mark what the younger self did not yet know. The middle is suspense of cause, not outcome. Close by returning to the older lead, one sentence longer than the opening.",
    },
    "conflict": {
        "internal_fear": "The obstacle lives inside the lead. Something in their past — a loss, a humiliation, a promise made to themselves — has taught them that wanting this person is dangerous. Every time the person they love steps closer, the lead flinches, deflects, invents reasons to leave the room. The person they love is willing; the lead is the wall. The setting the user gives keeps offering openings the lead refuses to walk through.",
        "misunderstanding": "A single wrong reading of a moment metastasizes. The lead sees the person they love in a gesture, a message, a company they keep, and constructs a story about what it means — a story that is not true. Both keep acting on their private version of events, each convinced the other has already decided something. The setting the user gives keeps handing them evidence that fits the wrong story more neatly than the right one.",
        "timing": "Neither of them is the problem; the calendar is. The lead is arriving at something — a departure, a commitment already made, a season of their life that has no room in it — exactly when the person they love appears. Feelings are not in question. What is in question is whether either of them can rearrange a life already in motion. The setting the user gives keeps ticking down around them.",
        "rivalry": "The lead and the person they love want the same thing and only one can have it — a role, a place, a recognition inside the setting the user gives. Every encounter is scored. Tenderness keeps surfacing in the exact moments when one of them has just cost the other something. Neither can tell anymore whether they are drawn to each other because of the contest or in spite of it, and the contest will not pause for them to find out.",
        "unfinished_history": "They already know each other. Something happened between them once — a parting, a betrayal, a promise nobody kept — and it was never closed, only walked away from. Now the setting the user gives has put them back in the same room, and every ordinary exchange carries the weight of the sentence neither of them finished. Before anything new can begin, the old thing has to be named out loud.",
        "secret": "The lead is carrying something the person they love does not know — a fact about who the lead is, why they came to the setting the user gives, or what they have already done. Every warm moment is shadowed by the calculation of when, or whether, to tell. The longer the silence holds, the more the eventual telling will cost, and the lead can feel the ledger growing under every conversation.",
        "ambition_clash": "Each of them is pointed at a life the other cannot follow into. The lead's work, calling, or chosen future pulls in one direction; the person they love is built for another. Neither is willing to be the one who shrinks. The setting the user gives keeps forcing small choices — whose evening, whose city, whose plan — that are really the big choice in disguise, and both of them know it.",
        "social_cost": "Being together in the open would cost them something the world around them charges — family standing, a community's approval, a place inside the setting the user gives that neither can afford to lose. In private the feeling is uncomplicated. In public they perform distance. The pressure is not that they doubt each other; it is that everyone else will make them pay, and one of them will have to decide first whether the price is bearable.",
        "timing_erosion": "Nothing dramatic is wrong. The lead and the person they love have been near each other long enough that the small frictions of the setting the user gives — the unspoken resentments, the postponed conversations, the way ordinary days sand down attention — have quietly worn the connection thin. The conflict is whether either of them still notices in time, and whether noticing is enough when the damage is made of a thousand ordinary evenings.",
    },
    "resolution": {
        "grand_gesture_then_growth": "The lead makes one large, unmistakable move toward the person they love — a public arrival, a spent savings, a burnt bridge behind them. The gesture lands, but the story does not end there. The final beats show them a season later inside the setting the user gives, still doing the small unglamorous work the gesture only promised.",
        "open_ended": "The lead and the person they love are left mid-motion inside the setting the user gives — a doorway half-crossed, a message unsent, a phone lit but unanswered. The story refuses to commit to together or apart. The final image is deliberately ambiguous, weighted toward one reading but never confirming it, leaving the reader to finish the sentence themselves.",
        "quiet_convergent": "No speech, no gesture large enough to name. The lead and the person they love simply end up in the same small act inside the setting the user gives — folding the same laundry, walking the same route, sharing a cigarette on the same step. What was misaligned in the middle has quietly aligned, and neither of them remarks on it. The reader feels the resolution before the characters do.",
        "un_healed": "The wound the story opened stays open. The lead and the person they love reach an honest accounting of what broke and why, but nothing repairs. The final scene inside the setting the user gives shows them changed but not mended — a routine resumed with a limp, a room re-entered with a smaller voice. The story insists some things do not close.",
        "recontextualize": "Nothing changes in the outer situation between the lead and the person they love — the same setting the user gives, the same arrangement, the same daily shape. What changes is how the lead sees it. A single late detail reframes everything preceding, and the final beat is the same room read differently. The resolution lives entirely inside the lead's understanding.",
        "earned": "The lead and the person they love arrive at each other only after paying visible costs — apologies made in full sentences, habits actually dropped, a third party told the truth. The final scene inside the setting the user gives is small and domestic, but the reader has watched every brick get laid. There is no shortcut, no last-minute grace; the ending feels bought.",
        "reconcile_or_release": "The lead reaches a clean binary with the person they love: fully back in, or fully let go. Whichever it is, it is chosen out loud inside the setting the user gives, with the other person present. No hedging, no maybe-later. The final beats show the lead beginning to live inside that choice — the first morning after, unmistakably one thing or the other.",
        "choose_and_lose": "The lead is forced to pick between the person they love and something else the story has made equally sacred — a place, a duty, a version of themselves. They choose, and the story honors the cost of what they did not choose. The final image inside the setting the user gives holds both the gain and the absence in the same frame, without softening either.",
        "quiet": "The story ends on a low, ordinary beat inside the setting the user gives — a light switched off, a plate rinsed, a name said once at normal volume. The lead and the person they love are neither reunited nor parted with fanfare; the emotional work has already happened offstage, and this is only the exhale. The reader leaves on a held breath, not a chord.",
    },
    "pov": {
        "first": "Tell the entire story from inside the lead's head, using \"I\" and \"me\" in every scene without exception. The reader only knows what the lead notices, misreads, or refuses to look at directly. Interiority runs hot: half-thoughts, self-corrections, small lies to themselves. The person they love and the setting the user gives arrive only through the lead's senses and slanted judgments, never neutrally.",
        "dual_alternating": "Split the narration between two \"I\" voices, alternating strictly by scene: the lead, then the person they love, then the lead again. Each voice sees the same setting the user gives through incompatible assumptions, so a moment one narrates as tenderness the other narrates as warning. Withhold from each what the other knows. The reader triangulates the truth that neither speaker will say out loud.",
        "retrospective_first": "The lead narrates in first person from years afterward, in past tense, knowing already how it ended. Let the older voice intrude: dry corrections of the younger self, foreshadowing dropped in as parentheticals, tenderness for a version of themselves that couldn't yet see the person they love clearly. The setting the user gives is remembered, not lived — softened, sharpened, or misfiled by hindsight.",
        "close_third": "Third person throughout — \"she,\" \"he,\" \"they\" — but the camera never leaves the lead's shoulder. The reader hears the lead's thoughts unmarked, in free indirect style, so the prose itself takes on their vocabulary and blind spots. The person they love and the setting the user gives are rendered only as the lead perceives them. No head-hopping, no omniscient asides, no scene where the lead is absent.",
        "second_person": "Address the lead as \"you\" throughout, present tense, as if narrating their life back to them a half-step ahead of their own awareness. \"You\" walk into the setting the user gives; \"you\" mistake the person they love for someone safer. The pronoun stays locked — never slip into I or she. The effect is complicit, slightly accusatory, the reader pinned inside a life they didn't quite consent to inhabit.",
    },
}

# Per-value incompatibility notes captured from the axis authors — informational.
_AXIS_INCOMPAT_NOTES = {
    ("arc", "linear"): ["twist:time_skip", "twist:in_medias_res_open", "structure:framed_narrative", "opening:cold_open_mystery"],
    ("arc", "non_linear_flashback"): ["tense:past_throughout", "opening:ordinary_day_open", "twist:strictly_chronological", "structure:single_stream"],
    ("arc", "dual_timeline"): ["structure:single_stream", "twist:strictly_chronological", "pov:single_voice_locked", "opening:ordinary_day_open"],
    ("arc", "dual_pov"): ["pov:single_first_person", "pov:single_third_limited", "pov:omniscient", "structure:single_stream"],
    ("arc", "retrospective_first"): ["twist:ending_reveal", "twist:ambiguous_ending", "pov:third_limited_present", "opening:cold_open_mystery"],
    ("resolution", "grand_gesture_then_growth"): ["arc:non_linear_flashback"],
}


# ── Topic hints per family ─────────────────────────────────────────────────
_TOPIC_HINTS = {
    "romance": [
        (re.compile(r"(?i)\b(putus|perpisahan|pisah|mantan|patah\s*hati|berpisah|kandas|move\s*on)\b"),
         ["breakup_first", "the_almost", "slow_fade", "second_chance"]),
        (re.compile(r"(?i)\b(reuni|cinta\s*lama|masa\s*lalu|ketemu\s*lagi|jumpa\s*lagi|bertemu\s*kembali|nostalgia)\b"),
         ["second_chance", "breakup_first"]),
        (re.compile(r"(?i)\b(saingan|rival|lawan|kompetisi|lomba|rebutan|bersaing|kompetitor)\b"),
         ["rival_to_love"]),
        (re.compile(r"(?i)\b(rahasia|sembunyi|menyembunyikan|kejutan|tersembunyi)\b"),
         ["secret_reveal"]),
        (re.compile(r"(?i)\b(sahabat|teman|circle|geng|genk|kelompok|grup|komunitas|satu\s*circle)\b"),
         ["group_to_pair"]),
        (re.compile(r"(?i)\b(karir|karier|ambisi|mimpi|cita-cita|kerja|pekerjaan|beasiswa|impian)\b"),
         ["ambition_collision"]),
        (re.compile(r"(?i)\b(ldr|jarak\s*jauh|beda\s*kota|pindah|merantau|jauh)\b"),
         ["ambition_collision", "slow_fade", "second_chance"]),
        (re.compile(r"(?i)\b(nyaris|hampir|tak\s*sampai|tak\s*kesampaian|gagal\s*jadian)\b"),
         ["the_almost"]),
        # V2 additions:
        (re.compile(r"(?i)\b(comedic|comedy|romcom|rom-com|lucu|kocak|jenaka|absurd|farce)\b"),
         ["light_comedic"]),
        (re.compile(r"(?i)\b(tugas|dinas|panggilan|kewajiban|amanah|duty|conscript|orang\s*tua|adat)\b"),
         ["we_vs_world_close_third"]),
    ],
    "kdrama": [
        (re.compile(r"(?i)\b(chaebol|conglomerate|hostile\s+takeover|succession|corporate|company\s+war|boardroom|merger|acquisition)\b"),
         ["chaebol_contract", "corporate_thriller"]),
        (re.compile(r"(?i)\b(revenge|revenj|balas\s*dendam|return\s+to\s+destroy|vendetta|payback)\b"),
         ["revenge_return"]),
        (re.compile(r"(?i)\b(birth\s*secret|hidden\s*parentage|swapped|adopted|birth\s+order|makjang|makjjang|long-lost)\b"),
         ["birth_secret_makjang"]),
        (re.compile(r"(?i)\b(reaper|grim\s*reaper|immortal|god|goblin|curse|reincarnation|past\s+life|deity|spirit)\b"),
         ["fantasy_bond"]),
        (re.compile(r"(?i)\b(hospital|surgeon|doctor|law\s*firm|prosecutor|lawyer|newsroom|reporter|procedural)\b"),
         ["workplace_slow_burn", "terminal_melodrama"]),
        (re.compile(r"(?i)\b(second\s*lead|love\s*triangle|triangle|the\s+one\s+who\s+got\s+away)\b"),
         ["second_lead_triangle"]),
        (re.compile(r"(?i)\b(illness|terminal|cancer|dying|sakit|tumor|leukemia|sekarat)\b"),
         ["terminal_melodrama"]),
        (re.compile(r"(?i)\b(class\s*war|social\s*class|beda\s*kelas|poor\s+rich|elite|prole|underclass|corruption)\b"),
         ["class_war_romance", "chaebol_contract"]),
        (re.compile(r"(?i)\b(amnesia|memory\s*loss|forgot|hilang\s*ingatan|coma|blackout)\b"),
         ["amnesia_reset"]),
        (re.compile(r"(?i)\b(time\s*slip|time\s*travel|dulu|masa\s*depan|joseon|goryeo|era|era-crossing)\b"),
         ["timeslip_fate", "fantasy_bond"]),
        (re.compile(r"(?i)\b(family\s*saga|ensemble|three\s+generations|siblings|father\s+and\s+son|weekend\s*drama)\b"),
         ["ensemble_family_saga"]),
        # V2 additions:
        (re.compile(r"(?i)\b(healing|slice[\s-]*of[\s-]*life|bakery|bookshop|clinic|soup\s*kitchen|hearth|comfort)\b"),
         ["healing_slice"]),
        (re.compile(r"(?i)\b(underdog|grassroots|movement|activist|whistleblower|community|uprising|triumph)\b"),
         ["uplifting_underdog"]),
    ],
}


def _family_keys(family: str) -> list:
    return [k for k, v in BEAT_MAPS.items() if v["family"] == family]


_MIN_POOL_SIZE = 4   # trap-topic diversification guard (adversarial audit finding)


def compatible_beatmaps(topic, family="romance"):
    """Return topic-compatible beat-map ids from the family. When the topic-hint pool is
    narrower than _MIN_POOL_SIZE, top up with random family peers so single-hint topics
    (e.g. 'balas dendam' → [revenge_return]) don't collapse to a deterministic pool.

    Failure mode this fixes (audit lens-3): a single-hint topic whose sole match is a
    signature-twist preset produces byte-identical structural output on every roll —
    anti-repeat can't rescue because `fresh or pool` fallback restores the length-1 pool.
    Top-up preserves targeting intent (hint matches come first) while guaranteeing
    variety when the operator publishes many narasi on one narrow topic."""
    keys = _family_keys(family)
    t = (topic or "").lower()
    matched: list = []
    for rx, ks in _TOPIC_HINTS.get(family, []):
        if rx.search(t):
            matched.extend(k for k in ks if k in keys)
    seen = set()
    ordered = [k for k in matched if not (k in seen or seen.add(k))]
    if not ordered:
        return keys
    if len(ordered) >= _MIN_POOL_SIZE:
        return ordered
    # top up with random family peers, not in the hint-matched set, to reach MIN_POOL_SIZE.
    # Deterministic pick (sorted by key) so tests + telemetry stay stable across restarts;
    # anti-repeat downstream still rotates freely.
    peers = [k for k in sorted(keys) if k not in seen]
    need = _MIN_POOL_SIZE - len(ordered)
    return ordered + peers[:need]


# ── Twist slot (v2) ────────────────────────────────────────────────────────
_TWIST_WEIGHT = {
    "none": 0.60,
    "third_party_engineered": 0.08,
    "one_is_leaving_ill": 0.08,
    "parallel_relationship": 0.08,
    "time_skip_reveal": 0.08,
    "unreliable_narrator": 0.08,
}
_TWIST_NON_NONE_KEYS = ("third_party_engineered", "one_is_leaving_ill",
                        "parallel_relationship", "time_skip_reveal",
                        "unreliable_narrator")


def _twist_weights_current() -> dict:
    """Return _TWIST_WEIGHT with NARASI_TWIST_WEIGHT_NONE env override applied.
    Env value clamped to [0.0, 1.0]; invalid / unset → default 0.60. Remaining
    weight (1 − none_share) is split evenly across the 5 non-'none' twist slots.
    Read at call time so Railway env updates pick up without a service restart."""
    raw = (os.environ.get("NARASI_TWIST_WEIGHT_NONE") or "").strip()
    if not raw:
        return dict(_TWIST_WEIGHT)
    try:
        none_share = float(raw)
    except (ValueError, TypeError):
        return dict(_TWIST_WEIGHT)
    if not (0.0 <= none_share <= 1.0):
        return dict(_TWIST_WEIGHT)
    other = (1.0 - none_share) / len(_TWIST_NON_NONE_KEYS)
    w = {"none": none_share}
    for k in _TWIST_NON_NONE_KEYS:
        w[k] = other
    return w
_PRESET_HAS_SIGNATURE_TWIST = frozenset({
    "secret_reveal",
    "birth_secret_makjang", "revenge_return", "terminal_melodrama",
    "amnesia_reset", "timeslip_fate", "fantasy_bond",
})


def select_twist(beatmap_id, *, recent_twist_ids=None, override=None, rng=None):
    if beatmap_id in _PRESET_HAS_SIGNATURE_TWIST:
        return None
    R = rng or random
    if override:
        ov = str(override).strip().lower()
        if ov == "none":
            return None
        if ov in TWIST_SLOTS:
            slot = TWIST_SLOTS[ov]
            if beatmap_id not in (slot.get("incompatible_with") or []):
                return {**slot, "twist_id": ov}
    recent = set(recent_twist_ids or [])
    weights = _twist_weights_current()
    for r in recent:
        if r in weights and r != "none":
            weights[r] *= 0.25
    for tid, slot in TWIST_SLOTS.items():
        if beatmap_id in (slot.get("incompatible_with") or []):
            weights[tid] = 0.0
    total = sum(weights.values())
    if total <= 0:
        return None
    r = R.random() * total
    cum = 0.0
    for k, w in weights.items():
        cum += w
        if r <= cum:
            if k == "none":
                return None
            return {**TWIST_SLOTS[k], "twist_id": k}
    return None


# ── Recombination engine (romance) ─────────────────────────────────────────
# Cross-axis incompatibilities (rejected combos). Curated + workflow-derived.
_VALIDITY_INCOMPAT_ROMANCE = {
    ("arc", "dual_pov", "pov", "close_third"): "dual-POV wants dual_alternating",
    ("arc", "dual_pov", "pov", "first"): "dual-POV wants dual_alternating",
    ("arc", "dual_pov", "pov", "retrospective_first"): "dual-POV wants dual_alternating",
    ("arc", "dual_pov", "pov", "second_person"): "dual-POV × second-person is odd",
    ("arc", "linear", "pov", "dual_alternating"): "dual_alternating implies dual_pov arc",
    ("arc", "retrospective_first", "pov", "close_third"): "retro-first names its POV",
    ("arc", "retrospective_first", "pov", "dual_alternating"): "retro-first names its POV",
    ("arc", "retrospective_first", "pov", "second_person"): "retro-first names its POV",
    ("arc", "non_linear_flashback", "conflict", "timing_erosion"): "erosion needs linear time",
    ("arc", "dual_timeline", "conflict", "misunderstanding"): "dual-timeline wants history not miscomm",
    ("resolution", "choose_and_lose", "conflict", "internal_fear"): "choose-and-lose specific to ambition",
    ("resolution", "choose_and_lose", "conflict", "misunderstanding"): "choose-and-lose specific to ambition",
    ("resolution", "choose_and_lose", "conflict", "timing"): "choose-and-lose specific to ambition",
    ("resolution", "choose_and_lose", "conflict", "rivalry"): "choose-and-lose specific to ambition",
    ("resolution", "choose_and_lose", "conflict", "social_cost"): "choose-and-lose specific to ambition",
    ("resolution", "reconcile_or_release", "conflict", "internal_fear"): "specific to unfinished-history",
    ("resolution", "reconcile_or_release", "conflict", "misunderstanding"): "specific to unfinished-history",
    ("resolution", "reconcile_or_release", "conflict", "timing"): "specific to unfinished-history",
    ("resolution", "reconcile_or_release", "conflict", "rivalry"): "specific to unfinished-history",
    ("resolution", "recontextualize", "conflict", "internal_fear"): "recontextualize needs a secret",
    ("resolution", "recontextualize", "conflict", "misunderstanding"): "recontextualize needs a secret",
    ("resolution", "recontextualize", "conflict", "rivalry"): "recontextualize needs a secret",
    ("resolution", "recontextualize", "conflict", "timing"): "recontextualize needs a secret",
    ("resolution", "un_healed", "conflict", "rivalry"): "rivalry earns respect, not un-healed",
    ("resolution", "grand_gesture_then_growth", "conflict", "timing_erosion"): "gesture doesn't fit erosion",
    ("resolution", "grand_gesture_then_growth", "conflict", "timing"): "grand-gesture can't beat timing",
    ("resolution", "grand_gesture_then_growth", "conflict", "unfinished_history"): "history needs release/reconcile",
    ("resolution", "grand_gesture_then_growth", "conflict", "ambition_clash"): "ambition needs choose-and-lose",
}


def compose_beatmap_romance(axes, *, override_id=None, rng=None):
    """Free composition — assemble a beat-map from axis fragments. None if invalid."""
    for a in ("arc", "conflict", "resolution", "pov"):
        if a not in axes:
            return None
        if axes[a] not in AXIS_FRAGMENTS.get(a, {}):
            return None
    for (a1, v1, a2, v2), _r in _VALIDITY_INCOMPAT_ROMANCE.items():
        if axes.get(a1) == v1 and axes.get(a2) == v2:
            return None
        if axes.get(a2) == v1 and axes.get(a1) == v2:
            return None
    body = ["STRUKTUR CERITA (compose):"]
    for axis in ("arc", "conflict", "resolution", "pov"):
        body.append(AXIS_FRAGMENTS[axis][axes[axis]])
    trust = ("TRUST THE READER — no real-world statistic citation, no inline term "
             "glossary, no genre self-reference. Be the story, don't narrate it.")
    key = override_id or ("compose_" + "_".join(axes[a] for a in ("arc", "conflict", "resolution", "pov")))
    return {
        "beatmap_id": key,
        "family": "romance",
        "display_name": f"Compose [{axes['arc']} · {axes['conflict']} · {axes['resolution']} · {axes['pov']}]",
        "tone": "composed",
        "axes": dict(axes),
        "structural_prompt": "\n\n".join(body) + "\n\n" + trust,
        "chapter_spine": _generic_6_spine(axes),
    }


def _generic_6_spine(axes):
    return [
        "Bab 1 — Establish: baseline given the ARC + POV",
        "Bab 2 — Force the CONFLICT into the open; first pressure",
        "Bab 3 — Escalate: pressures compound; POV interiority deepens",
        "Bab 4 — Peak of the conflict; a decisive scene",
        "Bab 5 — Turn per the RESOLUTION shape",
        "Bab 6 — Land: the resolution's aftermath",
    ]


def valid_combo_count_romance():
    """Rough valid-combo count (post-validity-prune) for romance recombination."""
    n_arc, n_cf, n_res, n_pov = 5, 9, 9, 5
    raw = n_arc * n_cf * n_res * n_pov
    return int(raw * 0.7)   # ≈ 1417


def _emit(key):
    bm = BEAT_MAPS[key]
    return {
        "beatmap_id": key,
        "family": bm["family"],
        "display_name": bm["display_name"],
        "tone": bm["tone"],
        "axes": dict(bm["axes"]),
        "structural_prompt": bm["structural_prompt"],
        "chapter_spine": list(bm["chapter_spine"]),
    }


def _emit_with_twist(bm, twist=None):
    """Wrap emitted beatmap with an optional twist overlay + telemetry field."""
    out = dict(bm)
    if twist:
        out["twist_id"] = twist.get("twist_id")
        out["structural_prompt"] = (
            out["structural_prompt"].rstrip()
            + "\n\nTWIST OVERLAY:\n"
            + (twist.get("twist_fragment") or "").strip()
        )
    else:
        out["twist_id"] = None
    return out


def select_beatmap(topic, style, *, tenant_id=None, override=None, twist_override=None,
                    mode="preset", recent_ids=None, recent_twist_ids=None):
    """Extended selector.
      mode='preset'  — pick from the 26 named presets (topic-aware + anti-repeat).
      mode='compose' — free-compose from axis fragments (romance family only).
      mode='off'     — return None.
      twist_override — force a twist_id ('none' disables layering; unknown = ignored).
    """
    if mode == "off":
        return None
    family = beatmap_family_for_style(style)
    if not family:
        return None
    bm = None
    if mode == "compose" and family == "romance":
        for _ in range(20):
            axes = {
                "arc": random.choice(list(AXIS_FRAGMENTS["arc"].keys())),
                "conflict": random.choice(list(AXIS_FRAGMENTS["conflict"].keys())),
                "resolution": random.choice(list(AXIS_FRAGMENTS["resolution"].keys())),
                "pov": random.choice(list(AXIS_FRAGMENTS["pov"].keys())),
            }
            bm = compose_beatmap_romance(axes)
            if bm:
                break
        if not bm:
            return None
    else:
        if override:
            ov = str(override).strip().lower()
            if ov in BEAT_MAPS and BEAT_MAPS[ov]["family"] == family:
                bm = _emit(ov)
        if bm is None:
            pool = compatible_beatmaps(topic, family)
            recent = set(recent_ids or [])
            fresh = [k for k in pool if k not in recent] or pool
            bm = _emit(random.choice(fresh))
    twist = select_twist(bm["beatmap_id"], recent_twist_ids=recent_twist_ids, override=twist_override)
    return _emit_with_twist(bm, twist)


def structural_block(bm):
    if not bm:
        return ""
    return "STRUKTUR CERITA (beat-map) — WAJIB dipatuhi:\n" + (bm.get("structural_prompt") or "").strip() + "\n\n"
