# CATATAN — MIN_SAMPLE_MARKETS (Ambang Sampel Minimum Wallet)

> Dokumen untuk owner. Bahasa sederhana, tanpa jargon teknis berlebihan.
> Terkait: ADR-018 di `DECISIONS.md`, perubahan T-031.

## Apa itu MIN_SAMPLE_MARKETS?

`MIN_SAMPLE_MARKETS` adalah **jumlah minimal market yang sudah selesai
(resolve) yang harus dimainkan sebuah wallet** sebelum bot berani
menilai dan mengirim alert tentang wallet itu.

Kenapa perlu? Karena performa di sedikit market bisa jadi kebetulan.
Wallet yang main 3 market dan menang 3 (win rate 100%) belum tentu
pintar — bisa sekadar hoki. Wallet yang main 100 market dan menang 65%
dari harga rata-rata 0.50 itu jauh lebih kuat buktinya.

## Kenapa sekarang diubah dari 50 ke 10?

Data wallet nyata di DB saat keputusan dibuat (2026-11-09):

| Kategori | Jumlah wallet |
|---|---|
| Total wallet pernah trade | 1.891 |
| Pernah main ≥ 10 market resolve | 65 |
| Pernah main ≥ 50 market resolve | **0** |

Artinya: dengan ambang 50, **tidak ada satu pun wallet yang bisa
memicu alert** — alert otomatis mustahil menyala, dan kita tidak bisa
menguji alur alert dengan data nyata selama MVP. Ambang 10 membuat 65
wallet lolos ambang sehingga pipeline alert benar-benar teruji.

## Penyusutan (shrinkage) tetap menjaga kualitas

Ambang yang lebih rendah TIDAK berarti alert jadi ngawur. Setiap skor
tetap dikalikan faktor keyakinan `n / (n + k)` dengan `k = 50`
(`shrinkage_k` tidak diubah). Contoh untuk edge 20%:

| Market resolve (n) | Edge efektif setelah penyusutan |
|---|---|
| 5 | 20% × 5/55 = **1,8%** |
| 10 | 20% × 10/60 = **3,3%** |
| 20 | 20% × 20/70 = **5,7%** |
| 50 | 20% × 50/100 = **10,0%** |
| 100 | 20% × 100/150 = **13,3%** |

Jadi wallet dengan 10 market harus tampil SANGAT bagus (edge mentah
besar) agar skornya cukup untuk alert — penyusutan meredam "hoki
amplop kecil".

## Trade-off ambang: 50 vs 20 vs 10

| Ambang | Kecepatan alert hidup | Risiko | Catatan |
|---|---|---|---|
| 50 | Sangat lambat (0 wallet lolos hari ini) | Hampir tanpa false positive | Cocok saat uang sungguhan |
| 20 | Lambat (masih sangat sedikit wallet) | Rendah | Kompromi, tapi tetap sepi |
| **10 (MVP)** | Cepat — 65 wallet langsung lolos | Lebih berisik; diredam shrinkage k=50 | Membuat pipeline alert teruji nyata |

## Keputusan

| Fase | Ambang | Alasan |
|---|---|---|
| **MVP (sekarang)** | **10** | Alert bisa hidup dan diuji dengan data nyata; kualitas dijaga penyusutan |
| **v1 / v2 (uang sungguhan)** | **50** | False positive mahal saat copy-trade eksekusi nyata |

Parameter ini tidak di-hardcode di logika: `ScoringPolicy.min_sample`
bisa di-inject, jadi menaikkan kembali ke 50 di v1/v2 cukup lewat
konfigurasi tanpa mengubah kode penilaian.
