"""python/pakem/beatmaps.py — Beat-map library v1 (romance / coming-of-age family).

A beat-map governs PLOT ARCHITECTURE, orthogonal to `style` (which governs register). The
selected beat-map's `structural_prompt` is injected into OUTLINE generation so the SAME
style produces a DIFFERENT arc each time — fixing the corpus finding (sample-21/22/23) that
the pipeline had ONE romance skeleton, re-skinned per topic.

Flag-gated by DALANG_BEATMAP_ENABLED on the python service (default OFF -> byte-identical:
nothing here is imported/called unless the outline path opts in).

The 10 structural_prompts were authored + adversarially verified (workflow
`beatmap-romance-authoring`, 2026-07-06): 0 collapse into the default skeleton, all
topic-agnostic (use user-supplied setting/topic via slots) and register-clean (no inline
glossary, no stat-citation, POV locked per preset). Known texture-adjacencies (slow_fade ~
the_almost; breakup_first ~ second_chance) are mitigated by anti-repeat rotation. Portfolio
gaps (all first-person; earnest-to-melancholic tone; no we-vs-world obstacle) are tracked
for a follow-up expansion — see ~/docs/beatmap-library-v1-spec.md.

DO NOT hand-edit the BEAT_MAPS prompt strings — regenerate from the workflow result via
scratchpad/gen_beatmaps.py so long strings never suffer transcription drift.
"""
from __future__ import annotations

import os
import re
import random
from typing import Optional


def beatmaps_enabled() -> bool:
    """Master flag. OFF (default) -> the outline path never touches this module."""
    return os.environ.get("DALANG_BEATMAP_ENABLED") == "1"


# style registry_key -> beat-map family. Only these styles get a beat-map; everything
# else returns None (outline path stays byte-identical). K-drama family lands later.
_ROMANCE_STYLES = frozenset({
    "remaja_coming_of_age", "coming_of_age",
    "romance_contemporary", "romance",
})


def beatmap_family_for_style(style: Optional[str]) -> Optional[str]:
    s = (style or "").strip().lower()
    if s in _ROMANCE_STYLES:
        return "romance"
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
}


# ── topic-aware compatibility (spec section 6) ──────────────────────────────────
# A plot-specific topic must not be forced into a clashing beat-map (e.g. "perpisahan"
# should not become rival_to_love). Each hint maps a topic signal -> the FITTING subset.
# No hint matched -> the whole family is eligible (generic topic = wide open). Heuristic,
# free; the optional flash classifier for ambiguous titles is a later addition.
_TOPIC_HINTS = [
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
]


def _family_keys(family: str) -> list:
    return [k for k, v in BEAT_MAPS.items() if v["family"] == family]


def compatible_beatmaps(topic: Optional[str], family: str = "romance") -> list:
    """Return the beat-map keys that fit `topic` within `family`. No signal -> all keys."""
    keys = _family_keys(family)
    t = (topic or "").lower()
    matched: list = []
    for rx, ks in _TOPIC_HINTS:
        if rx.search(t):
            matched.extend(k for k in ks if k in keys)
    # dedupe preserving order; empty -> whole family eligible
    seen = set()
    ordered = [k for k in matched if not (k in seen or seen.add(k))]
    return ordered or keys


def _emit(key: str) -> dict:
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


def select_beatmap(topic: Optional[str], style: Optional[str], *,
                   tenant_id: Optional[str] = None,
                   override: Optional[str] = None,
                   recent_ids: Optional[list] = None) -> Optional[dict]:
    """Pick a beat-map for (topic, style). Pure function — the caller does Redis I/O and
    passes `recent_ids` (the tenant's last-used ids) for anti-repeat.

    - style not in a beat-map family -> None (outline stays default).
    - override (a valid beatmap_id in the resolved family) wins.
    - else: topic-compatible subset, minus recent_ids (reset if that empties it), random pick.
    """
    family = beatmap_family_for_style(style)
    if not family:
        return None
    if override:
        ov = str(override).strip().lower()
        if ov in BEAT_MAPS and BEAT_MAPS[ov]["family"] == family:
            return _emit(ov)
    pool = compatible_beatmaps(topic, family)
    recent = set(recent_ids or [])
    fresh = [k for k in pool if k not in recent] or pool
    return _emit(random.choice(fresh))


def structural_block(bm: dict) -> str:
    """Render the STORY STRUCTURE block injected into the outline user-turn."""
    if not bm:
        return ""
    return "STRUKTUR CERITA (beat-map) — WAJIB dipatuhi:\n" + (bm.get("structural_prompt") or "").strip() + "\n\n"
