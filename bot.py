import re
import sqlite3
from dataclasses import dataclass
from typing import Optional

import discord
from discord.ext import commands
from discord import app_commands

# =========================
# CONFIGURAÇÃO
# =========================

TOKEN = "COLOQUE_O_TOKEN_DO_BOT_AQUI"

GUILD_ID = 1458175649402454058
CANAL_APROVADOS_ID = 1484593179519881379
CANAL_RANKING_ID = 1490386122168209569
CARGO_RECRUTADOR_ID = 1477814598102155446

CARGOS_ALTOS_IDS = {
    1458952733494218912,
    1478243943488422040,
    1489691679635144936,
}

IGNORAR_IDS = {
    90931502673148703,
    145847022190304617,
    6996754268210004,
    1380658597993779441,
    1342551382900736082,
}

DB_FILE = "recrutadores.db"


# =========================
# BANCO DE DADOS
# =========================

def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS recrutador_stats (
            user_id INTEGER PRIMARY KEY,
            aprovacoes INTEGER NOT NULL DEFAULT 0
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS mensagens_processadas (
            message_id INTEGER PRIMARY KEY,
            approver_id INTEGER NOT NULL,
            approved_id INTEGER,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS config (
            chave TEXT PRIMARY KEY,
            valor TEXT NOT NULL
        )
    """)

    conn.commit()
    conn.close()


def ja_processada(message_id: int) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM mensagens_processadas WHERE message_id = ?",
        (message_id,)
    )
    row = cur.fetchone()
    conn.close()
    return row is not None


def registrar_aprovacao(message_id: int, approver_id: int, approved_id: Optional[int]) -> None:
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        INSERT OR IGNORE INTO recrutador_stats (user_id, aprovacoes)
        VALUES (?, 0)
    """, (approver_id,))

    cur.execute("""
        UPDATE recrutador_stats
        SET aprovacoes = aprovacoes + 1
        WHERE user_id = ?
    """, (approver_id,))

    cur.execute("""
        INSERT OR IGNORE INTO mensagens_processadas (message_id, approver_id, approved_id)
        VALUES (?, ?, ?)
    """, (message_id, approver_id, approved_id))

    conn.commit()
    conn.close()


def limpar_contagem() -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM recrutador_stats")
    cur.execute("DELETE FROM mensagens_processadas")
    conn.commit()
    conn.close()


def buscar_ranking(limit: int = 20) -> list[sqlite3.Row]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT user_id, aprovacoes
        FROM recrutador_stats
        ORDER BY aprovacoes DESC, user_id ASC
        LIMIT ?
    """, (limit,))
    rows = cur.fetchall()
    conn.close()
    return rows


def buscar_aprovacoes(user_id: int) -> int:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT aprovacoes
        FROM recrutador_stats
        WHERE user_id = ?
    """, (user_id,))
    row = cur.fetchone()
    conn.close()
    return row["aprovacoes"] if row else 0


def salvar_mensagem_painel(message_id: int) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO config (chave, valor)
        VALUES ('ranking_message_id', ?)
        ON CONFLICT(chave) DO UPDATE SET valor = excluded.valor
    """, (str(message_id),))
    conn.commit()
    conn.close()


def buscar_mensagem_painel() -> Optional[int]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT valor FROM config WHERE chave = 'ranking_message_id'")
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    try:
        return int(row["valor"])
    except ValueError:
        return None


# =========================
# UTILIDADES
# =========================

@dataclass
class ResultadoParse:
    approver_id: int
    approved_id: Optional[int]


def membro_autorizado(member: discord.Member) -> bool:
    ids = {role.id for role in member.roles}
    return bool(ids & CARGOS_ALTOS_IDS)


def tem_cargo_recrutador(member: Optional[discord.Member]) -> bool:
    if member is None:
        return False
    return any(role.id == CARGO_RECRUTADOR_ID for role in member.roles)


def extrair_ids_do_texto(texto: str) -> list[int]:
    encontrados = re.findall(r"<@!?(\d+)>", texto)
    return [int(x) for x in encontrados]


def parsear_mensagem_aprovacao(message: discord.Message) -> Optional[ResultadoParse]:
    for embed in message.embeds:
        titulo = (embed.title or "").lower()
        descricao = (embed.description or "").lower()
        if "erro" in titulo or "erro" in descricao:
            return None

    # 1) embed
    for embed in message.embeds:
        titulo = (embed.title or "").lower()
        descricao = embed.description or ""

        if "aprovado" in titulo or "aprovou o formulário" in descricao.lower():
            ids_mencionados = extrair_ids_do_texto(descricao)
            if len(ids_mencionados) >= 2:
                return ResultadoParse(
                    approver_id=ids_mencionados[0],
                    approved_id=ids_mencionados[1]
                )

    # 2) conteúdo
    content = message.content or ""
    if "aprovou o formulário" in content.lower():
        ids_mencionados = extrair_ids_do_texto(content)
        if len(ids_mencionados) >= 2:
            return ResultadoParse(
                approver_id=ids_mencionados[0],
                approved_id=ids_mencionados[1]
            )

    # 3) mentions
    if len(message.mentions) >= 2:
        return ResultadoParse(
            approver_id=message.mentions[0].id,
            approved_id=message.mentions[1].id
        )

    return None


async def tentar_processar_mensagem(message: discord.Message) -> bool:
    if message.channel.id != CANAL_APROVADOS_ID:
        return False

    if ja_processada(message.id):
        return False

    resultado = parsear_mensagem_aprovacao(message)
    if not resultado:
        return False

    approver_id = resultado.approver_id
    approved_id = resultado.approved_id

    if approver_id in IGNORAR_IDS:
        return False

    guild = message.guild
    if guild is None:
        return False

    approver_member = guild.get_member(approver_id)
    if approver_member is None:
        try:
            approver_member = await guild.fetch_member(approver_id)
        except discord.NotFound:
            return False

    if not tem_cargo_recrutador(approver_member):
        return False

    registrar_aprovacao(message.id, approver_id, approved_id)
    return True


def montar_embed_ranking(guild: discord.Guild) -> discord.Embed:
    rows = buscar_ranking(20)

    if not rows:
        descricao = "Nenhuma aprovação foi contabilizada ainda."
    else:
        linhas = []
        for pos, row in enumerate(rows, start=1):
            member = guild.get_member(row["user_id"])
            nome = member.mention if member else f"<@{row['user_id']}>"
            linhas.append(f"**{pos}º** — {nome} • `{row['aprovacoes']}` aprovações")
        descricao = "\n".join(linhas)

    embed = discord.Embed(
        title="🏆 Ranking de Recrutadores",
        description=descricao,
        color=0xFF6A00
    )
    embed.set_footer(text="Atualização automática • Mecânica BMI")
    return embed


async def atualizar_painel_ranking(guild: discord.Guild) -> None:
    canal = guild.get_channel(CANAL_RANKING_ID)
    if not isinstance(canal, discord.TextChannel):
        return

    embed = montar_embed_ranking(guild)
    message_id = buscar_mensagem_painel()

    if message_id:
        try:
            msg = await canal.fetch_message(message_id)
            await msg.edit(embed=embed, content=None)
            return
        except discord.NotFound:
            pass
        except discord.HTTPException:
            pass

    nova_msg = await canal.send(embed=embed)
    salvar_mensagem_painel(nova_msg.id)


# =========================
# BOT
# =========================

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready() -> None:
    print(f"Bot online como {bot.user}")

    guild = discord.Object(id=GUILD_ID)
    try:
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
        print(f"{len(synced)} comandos sincronizados.")
    except Exception as e:
        print(f"Erro ao sincronizar comandos: {e}")


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.guild and message.channel.id == CANAL_APROVADOS_ID:
        try:
            processou = await tentar_processar_mensagem(message)
            if processou:
                await atualizar_painel_ranking(message.guild)
                print(f"Mensagem processada: {message.id}")
        except Exception as e:
            print(f"Erro ao processar mensagem {message.id}: {e}")

    await bot.process_commands(message)


# =========================
# SLASH COMMANDS
# =========================

@bot.tree.command(name="ranking_recrutadores", description="Mostra o ranking dos recrutadores")
async def ranking_recrutadores(interaction: discord.Interaction) -> None:
    if not interaction.guild:
        await interaction.response.send_message("Use isso dentro do servidor.", ephemeral=True)
        return

    embed = montar_embed_ranking(interaction.guild)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="aprovacoes_recrutador", description="Mostra quantas aprovações um recrutador tem")
@app_commands.describe(usuario="Selecione o recrutador")
async def aprovacoes_recrutador(interaction: discord.Interaction, usuario: discord.Member) -> None:
    total = buscar_aprovacoes(usuario.id)

    embed = discord.Embed(
        title="📊 Aprovações do Recrutador",
        description=f"{usuario.mention} possui **{total}** aprovação(ões).",
        color=0xFF6A00
    )
    embed.set_footer(text="Sistema Mecânica BMI")

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="criar_painel_ranking", description="Cria ou recria o painel automático do ranking")
async def criar_painel_ranking(interaction: discord.Interaction) -> None:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Use isso dentro do servidor.", ephemeral=True)
        return

    if not membro_autorizado(interaction.user):
        await interaction.response.send_message("Você não tem permissão para usar este comando.", ephemeral=True)
        return

    await atualizar_painel_ranking(interaction.guild)
    await interaction.response.send_message("✅ Painel do ranking criado/atualizado.", ephemeral=True)


@bot.tree.command(name="recontar_recrutadores", description="Lê o canal de aprovados e refaz toda a contagem")
async def recontar_recrutadores(interaction: discord.Interaction) -> None:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Use isso dentro do servidor.", ephemeral=True)
        return

    if not membro_autorizado(interaction.user):
        await interaction.response.send_message("Você não tem permissão para usar este comando.", ephemeral=True)
        return

    canal = interaction.guild.get_channel(CANAL_APROVADOS_ID)
    if not isinstance(canal, discord.TextChannel):
        await interaction.response.send_message("Canal de aprovados não encontrado.", ephemeral=True)
        return

    await interaction.response.send_message(
        "Recontando aprovações... isso pode levar alguns segundos.",
        ephemeral=True
    )

    limpar_contagem()
    total = 0

    async for message in canal.history(limit=None, oldest_first=True):
        try:
            processou = await tentar_processar_mensagem(message)
            if processou:
                total += 1
        except Exception as e:
            print(f"Erro ao recontar mensagem {message.id}: {e}")

    await atualizar_painel_ranking(interaction.guild)

    await interaction.followup.send(
        f"✅ Recontagem concluída. `{total}` aprovação(ões) registradas.",
        ephemeral=True
    )


@bot.tree.command(name="resetar_recrutadores", description="Reseta a contagem dos recrutadores")
async def resetar_recrutadores(interaction: discord.Interaction) -> None:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Use isso dentro do servidor.", ephemeral=True)
        return

    if not membro_autorizado(interaction.user):
        await interaction.response.send_message("Você não tem permissão para usar este comando.", ephemeral=True)
        return

    limpar_contagem()
    await atualizar_painel_ranking(interaction.guild)

    await interaction.response.send_message("✅ Contagem resetada com sucesso.", ephemeral=True)


if __name__ == "__main__":
    init_db()
    bot.run(TOKEN)