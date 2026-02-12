# ChebulUP

## Надёжная передача данных по акустическому каналу

Проект демонстрирует построение надёжного транспортного уровня поверх нестабильного акустического канала передачи данных (через `ggwave`).

Фактически реализован стек:

```
Данные
  ↓
Фрейминг + CRC
  ↓
ARQ (Stop-and-Wait / Go-Back-N)
  ↓
ggwave (кодирование в звук)
  ↓
Звук
  ↓
ggwave (декодирование)
  ↓
ARQ
  ↓
Данные
```

---

# Цель проекта

Показать, что даже при:

* потере DATA = 25%
* потере ACK = 10%
* искажении DATA = 3%
* искажении ACK = 1%

можно добиться **100% надёжной доставки сообщения**.

---

# Архитектура

## 1. PHY (физический уровень)

* Библиотека `ggwave`
* Кодирование текста → звук
* Декодирование звук → текст
* Измерение длительности передачи

## 2. Packet Layer

* Разбиение на фреймы
* Заголовок (msg_id, seq, total)
* CRC-проверка
* Сборка сообщения из частей

## 3. ARQ-протоколы

### Stop-and-Wait

* Один кадр в полёте
* Таймаут → повтор
* Простая и надёжная модель

### Go-Back-N

* Скользящее окно (window=4)
* Несколько кадров одновременно
* При ошибке — повтор с base

## 4. Канал

Моделируется:

* drop_data
* drop_ack
* corrupt_data
* corrupt_ack

---

# Структура репозитория

```
scripts/
│
├── baseline_ggwave_file.py     # PHY-уровень (ggwave encode/decode)
├── ggwave_codec.py             # Вспомогательные функции кодирования
├── packet.py                   # Формирование и разбор фреймов + CRC
│
├── arq_stop_and_wait.py        # Реализация Stop-and-Wait
├── arq_gbn.py                  # Реализация Go-Back-N
│
├── measure_arq_fast_sim.py     # Быстрый симулятор без DSP
├── measure_arq_ggwave_smoke.py # Реальные тесты Stop-and-Wait
├── measure_gbn_smoke.py        # Реальные тесты Go-Back-N
├── measure_compare_smoke.py    # Сравнение протоколов
│
└── config.py                   # Общие параметры
```

---

# Быстрый старт

## 1. Установка

Создать виртуальное окружение:

```bash
python -m venv .venv
source .venv/bin/activate
```

Установить зависимости:

```bash
pip install ggwave
```

---

## 2. Проверка PHY

```bash
python -m scripts.baseline_ggwave_file
```

Ожидаемый результат:

```
Decoded: b'abc'
OK
```

---

## 3. Быстрый симулятор (без DSP)

Очень быстро проверяет параметры протоколов:

```bash
python -m scripts.measure_arq_fast_sim
```

---

## 4. Stop-and-Wait (реальный ggwave)

```bash
python -m scripts.measure_arq_ggwave_smoke
```

---

## 5. Go-Back-N

```bash
python -m scripts.measure_gbn_smoke
```

---

## 6. Сравнение протоколов

```bash
python -m scripts.measure_compare_smoke
```

---

# Результаты (payload = 130B)

Условия:

* DATA drop: 25%
* ACK drop: 10%
* DATA corrupt: 3%
* ACK corrupt: 1%
* max_payload = 32
* timeout = 0.2

## Stop-and-Wait

* Надёжность: **100%**
* PHY goodput: ~900–1500 B/s
* Virtual goodput: ~2.2 B/s
* Повторы: ~2–3

## Go-Back-N (window=4)

* Надёжность: **100%**
* PHY goodput: ~1400 B/s
* Virtual goodput: ~1.1–1.3 B/s
* Таймауты: ~2

---

# Выводы

* Надёжность достигнута при тяжёлых условиях потерь.
* Go-Back-N лучше использует физический канал.
* Stop-and-Wait проще и устойчивее при высокой доле потерь.
* Виртуальная метрика показывает влияние таймаутов на общую задержку.

---

# Что демонстрирует проект

* Реализацию ARQ поверх нестандартного физического канала
* Работающий стек передачи данных
* Поведение транспортных протоколов при потерях
* Практическую разницу между Stop-and-Wait и Go-Back-N

---

# Возможные улучшения

* Selective Repeat
* Адаптивный таймаут
* Динамическое окно
* Сжатие (Huffman)
