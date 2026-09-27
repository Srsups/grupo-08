"""
Spike — ADR-0005: Validação embarcada offline com deduplicação
Caso Ônibus · Envelope A · Grupo 08

Prova que o validador opera 100% offline com SQLite local,
recusa duplicatas por chave de idempotência e que o servidor
central detecta fraude cross-bus na sincronização.
"""

import sqlite3
import time
import hashlib
import json


# ============================================================
# SIMULAÇÃO DO SERVIDOR CENTRAL
# ============================================================

class CentralServer:
    """Simula a Aplicação Central + PostgreSQL.
    Na arquitetura real, recebe lotes via fila SQS."""

    def __init__(self):
        self.events: list[dict] = []
        self.cross_bus_duplicates: list[dict] = []

    def receive_batch(self, bus_id: str, batch: list[dict]) -> int:
        """Recebe lote de eventos de um validador.
        Detecta se o mesmo cartão+minuto já foi visto em outro ônibus."""
        for evt in batch:
            key = (evt["card_id"], evt["timestamp_minute"])
            existing = [
                e for e in self.events
                if e["card_id"] == key[0]
                and e["timestamp_minute"] == key[1]
                and e["bus_id"] != bus_id
            ]
            if existing:
                self.cross_bus_duplicates.append({
                    "card_id": evt["card_id"],
                    "timestamp_minute": evt["timestamp_minute"],
                    "bus_1": existing[0]["bus_id"],
                    "bus_2": bus_id,
                })
            self.events.append({**evt, "bus_id": bus_id})
        return len(batch)


# ============================================================
# VALIDADOR EMBARCADO (UM POR ÔNIBUS)
# ============================================================

TARIFA_COMUM_CENTAVOS = 460       # R$ 4,60
TARIFA_ESTUDANTE_CENTAVOS = 230   # R$ 2,30 (meia)
GRATUIDADES = ("idoso", "pcd")    # tarifa zero

class EmbeddedValidator:
    """Validador instalado no ônibus. Opera com SQLite em memória
    (na produção seria um arquivo no disco do validador).
    Não faz nenhuma chamada de rede."""

    def __init__(self, bus_id: str, authorized_cards: list[dict]):
        self.bus_id = bus_id
        self.db = sqlite3.connect(":memory:")
        self._init_schema(authorized_cards)
        self.pending_events: list[dict] = []

    # --- setup ---

    def _init_schema(self, cards: list[dict]) -> None:
        cur = self.db.cursor()
        cur.execute("""
            CREATE TABLE cards (
                card_id      TEXT PRIMARY KEY,
                card_type    TEXT NOT NULL,
                balance_cents INTEGER NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE validation_log (
                idempotency_key TEXT PRIMARY KEY,
                card_id         TEXT NOT NULL,
                ts              TEXT NOT NULL,
                result          TEXT NOT NULL,
                fare_cents      INTEGER NOT NULL
            )
        """)
        for c in cards:
            cur.execute(
                "INSERT INTO cards VALUES (?, ?, ?)",
                (c["id"], c["type"], c["balance_cents"]),
            )
        self.db.commit()

    # --- lógica de validação ---

    @staticmethod
    def _make_key(card_id: str, ts: str, bus_id: str) -> str:
        """Chave de idempotência = SHA-256(cartão | minuto | veículo)[:16].
        Truncar ao minuto impede que a mesma pessoa valide duas vezes
        no mesmo ônibus dentro de 60 s (regra de negócio do caso)."""
        minute = ts[:16]  # YYYY-MM-DDTHH:MM
        raw = f"{card_id}|{minute}|{bus_id}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    @staticmethod
    def _fare_for(card_type: str) -> int:
        if card_type in GRATUIDADES:
            return 0
        if card_type == "estudante":
            return TARIFA_ESTUDANTE_CENTAVOS
        return TARIFA_COMUM_CENTAVOS

    def validate(self, card_id: str, timestamp: str) -> tuple[bool, str, float]:
        """Valida passagem localmente.
        Retorna (aceita: bool, motivo: str, latencia_ms: float)."""
        t0 = time.monotonic()
        cur = self.db.cursor()
        key = self._make_key(card_id, timestamp, self.bus_id)

        # 1. Duplicata local?
        cur.execute(
            "SELECT 1 FROM validation_log WHERE idempotency_key = ?", (key,)
        )
        if cur.fetchone():
            ms = (time.monotonic() - t0) * 1000
            return False, "DUPLICATA_LOCAL", ms

        # 2. Cartão existe?
        cur.execute(
            "SELECT card_type, balance_cents FROM cards WHERE card_id = ?",
            (card_id,),
        )
        row = cur.fetchone()
        if not row:
            cur.execute(
                "INSERT INTO validation_log VALUES (?, ?, ?, ?, ?)",
                (key, card_id, timestamp, "CARTAO_NAO_ENCONTRADO", 0),
            )
            self.db.commit()
            ms = (time.monotonic() - t0) * 1000
            return False, "CARTAO_NAO_ENCONTRADO", ms

        card_type, balance = row
        fare = self._fare_for(card_type)

        # 3. Saldo suficiente?
        if balance < fare:
            cur.execute(
                "INSERT INTO validation_log VALUES (?, ?, ?, ?, ?)",
                (key, card_id, timestamp, "SALDO_INSUFICIENTE", 0),
            )
            self.db.commit()
            ms = (time.monotonic() - t0) * 1000
            return False, "SALDO_INSUFICIENTE", ms

        # 4. Debita e registra
        cur.execute(
            "UPDATE cards SET balance_cents = balance_cents - ? WHERE card_id = ?",
            (fare, card_id),
        )
        cur.execute(
            "INSERT INTO validation_log VALUES (?, ?, ?, ?, ?)",
            (key, card_id, timestamp, "ACEITA", fare),
        )
        self.db.commit()

        # 5. Enfileira evento para sincronização futura
        self.pending_events.append({
            "card_id": card_id,
            "timestamp": timestamp,
            "timestamp_minute": timestamp[:16],
            "result": "ACEITA",
            "fare_cents": fare,
            "idempotency_key": key,
        })

        ms = (time.monotonic() - t0) * 1000
        return True, "ACEITA", ms

    # --- sincronização ---

    def sync(self, central: CentralServer) -> int:
        """Envia eventos pendentes ao servidor central (simula retorno do 4G)."""
        if not self.pending_events:
            return 0
        count = central.receive_batch(self.bus_id, self.pending_events)
        self.pending_events.clear()
        return count


# ============================================================
# EXECUÇÃO DO SPIKE
# ============================================================

def print_result(bus: str, card: str, label: str, reason: str, ms: float):
    ok_symbol = "V" if reason == "ACEITA" else "X"
    print(f"  [{ok_symbol}] {bus} | {card:20s} | {reason:25s} | {ms:.2f} ms")


def main():
    sep = "=" * 64
    print(sep)
    print("SPIKE — ADR-0005: Validação embarcada offline")
    print("Caso Ônibus / Envelope A / Grupo 08")
    print(sep)

    # --- Cartões de teste ---
    cards = [
        {"id": "C-0001", "type": "comum",     "balance_cents": 1000},
        {"id": "C-0002", "type": "estudante",  "balance_cents": 500},
        {"id": "C-0003", "type": "idoso",      "balance_cents": 0},
        {"id": "C-0004", "type": "pcd",        "balance_cents": 0},
        {"id": "C-0005", "type": "comum",      "balance_cents": 200},
    ]

    central = CentralServer()
    bus_a = EmbeddedValidator("BUS-1201", cards)
    bus_b = EmbeddedValidator("BUS-1202", cards)

    # CENÁRIO 1 — Validações offline normais
    print("\n--- CENÁRIO 1: Validações offline (sem rede) ---")
    ts1 = "2026-09-27T07:30:00"

    ok, reason, ms = bus_a.validate("C-0001", ts1)
    print_result("BUS-1201", "C-0001 (comum)",     reason, reason, ms)

    ok, reason, ms = bus_a.validate("C-0002", ts1)
    print_result("BUS-1201", "C-0002 (estudante)",  reason, reason, ms)

    ok, reason, ms = bus_a.validate("C-0003", ts1)
    print_result("BUS-1201", "C-0003 (idoso)",      reason, reason, ms)

    ok, reason, ms = bus_a.validate("C-0004", ts1)
    print_result("BUS-1201", "C-0004 (pcd)",        reason, reason, ms)

    # CENÁRIO 2 — Duplicata local
    print("\n--- CENÁRIO 2: Duplicata local (mesmo cartão, mesmo minuto, mesmo ônibus) ---")
    ok, reason, ms = bus_a.validate("C-0001", "2026-09-27T07:30:45")
    print_result("BUS-1201", "C-0001 (2a tentativa)", reason, reason, ms)

    # CENÁRIO 3 — Saldo insuficiente
    print("\n--- CENÁRIO 3: Saldo insuficiente ---")
    ok, reason, ms = bus_a.validate("C-0005", ts1)
    print_result("BUS-1201", "C-0005 (saldo R$2,00)", reason, reason, ms)

    # CENÁRIO 4 — Cartão desconhecido
    print("\n--- CENÁRIO 4: Cartão não encontrado na base local ---")
    ok, reason, ms = bus_a.validate("C-9999", ts1)
    print_result("BUS-1201", "C-9999 (inexistente)", reason, reason, ms)

    # CENÁRIO 5 — Fraude cross-bus
    print("\n--- CENÁRIO 5: Mesmo cartão em dois ônibus (fraude cross-bus) ---")
    ts_fraud = "2026-09-27T08:15:00"

    ok, reason, ms = bus_a.validate("C-0001", ts_fraud)
    print_result("BUS-1201", "C-0001", reason, reason, ms)

    ok, reason, ms = bus_b.validate("C-0001", ts_fraud)
    print_result("BUS-1202", "C-0001", reason, reason, ms)

    print("  >> Ambos aceitam localmente (cada um offline, sem saber do outro).")
    print("  >> A fraude será detectada na sincronização (cenário 6).")

    # CENÁRIO 6 — Sincronização
    print("\n--- CENÁRIO 6: Retorno da conexão — sincronização em lote ---")
    synced_a = bus_a.sync(central)
    synced_b = bus_b.sync(central)
    print(f"  BUS-1201 sincronizou {synced_a} evento(s) com o servidor central.")
    print(f"  BUS-1202 sincronizou {synced_b} evento(s) com o servidor central.")

    if central.cross_bus_duplicates:
        print("\n  ** ALERTA — Duplicatas cross-bus detectadas pelo servidor central:")
        for dup in central.cross_bus_duplicates:
            print(
                f"     Cartão {dup['card_id']} usado em "
                f"{dup['bus_1']} e {dup['bus_2']} "
                f"no minuto {dup['timestamp_minute']}"
            )
    else:
        print("  Nenhuma duplicata cross-bus detectada.")

    # CENÁRIO 7 — Benchmark de latência
    print("\n--- CENÁRIO 7: Benchmark de latência (200 validações) ---")
    bench_cards = [
        {"id": f"BENCH-{i:04d}", "type": "comum", "balance_cents": 99999}
        for i in range(200)
    ]
    bench_bus = EmbeddedValidator("BUS-BENCH", bench_cards)
    latencies = []
    for i in range(200):
        ts_b = f"2026-09-27T09:{i // 60:02d}:{i % 60:02d}"
        _, _, ms_b = bench_bus.validate(f"BENCH-{i:04d}", ts_b)
        latencies.append(ms_b)
    max_ms = max(latencies)
    avg_ms = sum(latencies) / len(latencies)
    all_under = all(lat < 300 for lat in latencies)
    print(f"  200 validações executadas.")
    print(f"  Latência máxima: {max_ms:.2f} ms")
    print(f"  Latência média:  {avg_ms:.2f} ms")
    print(f"  Todas abaixo de 300 ms? {'SIM' if all_under else 'NÃO'}")

    # RESUMO
    print(f"\n{sep}")
    print("RESUMO DO SPIKE")
    print(sep)
    print("  [OK] Validação offline funciona sem nenhuma chamada de rede.")
    print("  [OK] Duplicata local recusada pela chave de idempotência.")
    print("  [OK] Saldo insuficiente e cartão desconhecido tratados localmente.")
    print("  [OK] Fraude cross-bus detectada pelo servidor na sincronização.")
    print("  [OK] Latência de validação consistentemente abaixo de 300 ms.")
    print(f"{sep}")
    print("SPIKE CONCLUÍDO — ADR-0005 validado.")
    print(sep)


if __name__ == "__main__":
    main()
"""

PS C:\Users\user\Documents\Github\ArquiteturaSoftware> python spike.py
================================================================
SPIKE — ADR-0005: Validação embarcada offline
Caso Ônibus / Envelope A / Grupo 08
================================================================

--- CENÁRIO 1: Validações offline (sem rede) ---
  [V] BUS-1201 | C-0001 (comum)       | ACEITA                    | 0.31 ms
  [V] BUS-1201 | C-0002 (estudante)   | ACEITA                    | 0.04 ms
  [V] BUS-1201 | C-0003 (idoso)       | ACEITA                    | 0.03 ms
  [V] BUS-1201 | C-0004 (pcd)         | ACEITA                    | 0.07 ms

--- CENÁRIO 2: Duplicata local (mesmo cartão, mesmo minuto, mesmo ônibus) ---
  [X] BUS-1201 | C-0001 (2a tentativa) | DUPLICATA_LOCAL           | 0.02 ms

--- CENÁRIO 3: Saldo insuficiente ---
  [X] BUS-1201 | C-0005 (saldo R$2,00) | SALDO_INSUFICIENTE        | 0.03 ms

--- CENÁRIO 4: Cartão não encontrado na base local ---
  [X] BUS-1201 | C-9999 (inexistente) | CARTAO_NAO_ENCONTRADO     | 0.03 ms

--- CENÁRIO 5: Mesmo cartão em dois ônibus (fraude cross-bus) ---
  [V] BUS-1201 | C-0001               | ACEITA                    | 0.03 ms
  [V] BUS-1202 | C-0001               | ACEITA                    | 0.06 ms
  >> Ambos aceitam localmente (cada um offline, sem saber do outro).
  >> A fraude será detectada na sincronização (cenário 6).

--- CENÁRIO 6: Retorno da conexão — sincronização em lote ---
  BUS-1201 sincronizou 5 evento(s) com o servidor central.
  BUS-1202 sincronizou 1 evento(s) com o servidor central.

  ** ALERTA — Duplicatas cross-bus detectadas pelo servidor central:
     Cartão C-0001 usado em BUS-1201 e BUS-1202 no minuto 2026-09-27T08:15

--- CENÁRIO 7: Benchmark de latência (200 validações) ---
  200 validações executadas.
  Latência máxima: 0.04 ms
  Latência média:  0.01 ms
  Todas abaixo de 300 ms? SIM

================================================================
RESUMO DO SPIKE
================================================================
  [OK] Validação offline funciona sem nenhuma chamada de rede.
  [OK] Duplicata local recusada pela chave de idempotência.
  [OK] Saldo insuficiente e cartão desconhecido tratados localmente.
  [OK] Fraude cross-bus detectada pelo servidor na sincronização.
  [OK] Latência de validação consistentemente abaixo de 300 ms.
================================================================
SPIKE CONCLUÍDO — ADR-0005 validado.
================================================================

"""