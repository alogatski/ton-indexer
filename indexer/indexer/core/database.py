import codecs
import asyncio
from copy import deepcopy
from time import sleep
from typing import List, Optional

from pytonlib.utils.tlb import parse_transaction
from pytonlib.utils.address import detect_address
from tvm_valuetypes.cell import deserialize_boc

from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from sqlalchemy_utils import create_database, database_exists, drop_database

from sqlalchemy import Column, String, Integer, BigInteger, Boolean, Index, Enum, Numeric
from sqlalchemy.schema import ForeignKeyConstraint
from sqlalchemy import ForeignKey, UniqueConstraint, Table, exc
from sqlalchemy import and_, or_, ColumnDefault
from sqlalchemy.orm import relationship, backref
from dataclasses import dataclass, asdict

from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import JSONB

from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

import asyncpg

from indexer.core.settings import Settings
from loguru import logger

MASTERCHAIN_INDEX = -1
MASTERCHAIN_SHARD = -9223372036854775808

settings = Settings()

# init database
def get_engine(settings: Settings):
    logger.critical(settings.pg_dsn)
    engine = create_async_engine(settings.pg_dsn, 
                                 pool_size=128, 
                                 max_overflow=24, 
                                 pool_timeout=128,
                                 echo=False)
    return engine

engine = get_engine(settings)

SessionMaker = sessionmaker(bind=engine, class_=AsyncSession)

# database
Base = declarative_base()

utils_url = str(engine.url).replace('+asyncpg', '')

def init_database(create=False):
    while not database_exists(utils_url):
        if create:
            logger.info('Creating database')
            create_database(utils_url)

            async def create_tables():
                async with engine.begin() as conn:
                    await conn.run_sync(Base.metadata.create_all)
            asyncio.run(create_tables())
        sleep(0.5)


# from sqlalchemy import event
# from sqlalchemy.engine import Engine
# import time
# import logging
# logger1 = logging.getLogger("myapp.sqltime")
# logger1.setLevel(logging.DEBUG)
# @event.listens_for(Engine, "before_cursor_execute")
# def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
#     conn.info.setdefault("query_start_time", []).append(time.time())
#     # logger1.debug(f"Start Query: {statement}")


# @event.listens_for(Engine, "after_cursor_execute")
# def after_cursor_execute(conn, cursor, statement, parameters, context, executemany):
#     total = time.time() - conn.info["query_start_time"].pop(-1)
#     logger1.debug(f"Query Complete: {statement}! Total Time: {total}")

@dataclass(init=False)
class Block(Base):
    __tablename__ = 'blocks'
    __table_args__ = (
        ForeignKeyConstraint(
            ["mc_block_workchain", "mc_block_shard", "mc_block_seqno"],
            ["blocks.workchain", "blocks.shard", "blocks.seqno"]
        ),
    )

    workchain: int = Column(Integer, primary_key=True)
    shard: int = Column(BigInteger, primary_key=True)
    seqno: int = Column(Integer, primary_key=True)
    root_hash: str = Column(String(44))
    file_hash: str = Column(String(44))

    mc_block_workchain: int = Column(Integer, nullable=True)
    mc_block_shard: str = Column(BigInteger, nullable=True)
    mc_block_seqno: int = Column(Integer, nullable=True)

    masterchain_block = relationship("Block", remote_side=[workchain, shard, seqno], backref='shard_blocks')

    global_id: int = Column(Integer)
    version: int = Column(Integer)
    after_merge: bool = Column(Boolean)
    before_split: bool = Column(Boolean)
    after_split: bool = Column(Boolean)
    want_split: bool = Column(Boolean)
    key_block: bool = Column(Boolean)
    vert_seqno_incr: bool = Column(Boolean)
    flags: int = Column(Integer)
    gen_utime: int = Column(BigInteger)
    start_lt: int = Column(BigInteger)
    end_lt: int = Column(BigInteger)
    validator_list_hash_short: int = Column(Integer)
    gen_catchain_seqno: int = Column(Integer)
    min_ref_mc_seqno: int = Column(Integer)
    prev_key_block_seqno: int = Column(Integer)
    vert_seqno: int = Column(Integer)
    master_ref_seqno: int = Column(Integer, nullable=True)
    rand_seed: str = Column(String(44))
    created_by: str = Column(String)

    transactions = relationship("Transaction", back_populates="block")


class Transaction(Base):
    __tablename__ = 'transactions'
    __table_args__ = (
        ForeignKeyConstraint(
            ["block_workchain", "block_shard", "block_seqno"],
            ["blocks.workchain", "blocks.shard", "blocks.seqno"]
        ),
    )

    block_workchain = Column(Integer)
    block_shard = Column(BigInteger)
    block_seqno = Column(Integer)

    block = relationship("Block", back_populates="transactions")

    account = Column(String)
    hash = Column(String, primary_key=True)
    utime = Column(Integer)
    lt = Column(BigInteger)

    transaction_type = Column(Enum('trans_storage', 'trans_ord', 'trans_tick_tock', \
        'trans_split_prepare', 'trans_split_install', 'trans_merge_prepare', 'trans_merge_install', name='trans_type'))

    account_state_hash_before = Column(String)#, ForeignKey('account_states.hash'))
    account_state_hash_after = Column(String)#, ForeignKey('account_states.hash'))

    old_account_state = relationship("AccountState", foreign_keys=[account_state_hash_before], viewonly=True)
    new_account_state = relationship("AccountState", foreign_keys=[account_state_hash_after], viewonly=True)

    fees = Column(BigInteger)
    storage_fees = Column(BigInteger)
    in_fwd_fees = Column(BigInteger)
    computation_fees = Column(BigInteger)
    action_fees = Column(BigInteger)

    compute_exit_code: int = Column(Integer)
    compute_gas_used: int = Column(BigInteger)
    compute_gas_limit: int = Column(BigInteger)
    compute_gas_credit: int = Column(BigInteger)
    compute_gas_fees: int = Column(BigInteger)
    compute_vm_steps: int = Column(BigInteger)
    compute_skip_reason: str = Column(Enum('cskip_no_state', 'cskip_bad_state', 'cskip_no_gas', name='compute_skip_reason_type'))

    action_result_code: int = Column(Integer)
    action_total_fwd_fees: int = Column(BigInteger)
    action_total_action_fees: int = Column(BigInteger)


class AccountState(Base):
    __tablename__ = 'account_states'

    hash = Column(String, primary_key=True)
    account = Column(String)
    balance = Column(BigInteger)
    account_status = Column(Enum('uninit', 'frozen', 'active', name='account_status_type'))
    frozen_hash = Column(String)
    code_hash = Column(String)
    data_hash = Column(String)

@dataclass(init=False)
class Message(Base):
    __tablename__ = 'messages'
    hash: str = Column(String(44), primary_key=True)
    source: str = Column(String)
    destination: str = Column(String)
    value: int = Column(BigInteger)
    fwd_fee: int = Column(BigInteger)
    ihr_fee: int = Column(BigInteger)
    created_lt: int = Column(BigInteger)
    created_at: int = Column(BigInteger)
    opcode: int = Column(Integer)
    ihr_disabled: bool = Column(Boolean)
    bounce: bool = Column(Boolean)
    bounced: bool = Column(Boolean)
    import_fee: int = Column(BigInteger)
    body_hash: str = Column(String(44))
    init_state_hash: str = Column(String(44))

class TransactionMessage(Base):
    __tablename__ = 'transaction_messages'
    transaction_hash = Column(String(44), ForeignKey('transactions.hash'), primary_key=True)
    message_hash = Column(String(44), ForeignKey('messages.hash'), primary_key=True)
    direction = Column(Enum('in', 'out', name="direction"), primary_key=True)

    transaction = relationship("Transaction", back_populates="messages")
    message = relationship("Message", back_populates="transactions")



@dataclass(init=False)
class MessageContent(Base):
    __tablename__ = 'message_contents'
    
    hash: int = Column(String, primary_key=True)
    body: str = Column(String)


class CodeHashInterfaces(Base):
    __tablename__ = 'code_hash'

    code_hash = Column(String, primary_key=True)
    interfaces = Column(ARRAY(Enum('nft_item', 
                                   'nft_editable', 
                                   'nft_collection', 
                                   'nft_royalty',
                                   'jetton_wallet', 
                                   'jetton_master',
                                   'domain',
                                   'subscription',
                                   'auction',
                                   name='interface_name')))

class JettonWallet(Base):
    __tablename__ = 'jetton_wallets'
    address = Column(String, primary_key=True)
    balance: int = Column(Numeric)
    owner = Column(String)
    jetton = Column(String)
    last_transaction_lt = Column(BigInteger)
    code_hash = Column(String)
    data_hash = Column(String)

class JettonMaster(Base):
    __tablename__ = 'jetton_masters'
    address = Column(String, primary_key=True)
    total_supply: int = Column(Numeric)
    mintable: bool = Column(Boolean)
    admin_address = Column(String, nullable=True)
    jetton_content = Column(JSONB, nullable=True)
    jetton_wallet_code_hash = Column(String)
    code_hash = Column(String)
    data_hash = Column(String)
    last_transaction_lt = Column(BigInteger)
    code_boc = Column(String)
    data_boc = Column(String)

class JettonTransfers(Base):
    __tablename__ = 'jetton_transfers'
    transaction_hash = Column(String, primary_key=True)
    query_id: int = Column(Numeric)
    amount: int = Column(Numeric)
    destination = Column(String)
    response_destination = Column(String)
    custom_payload = Column(String)
    forward_ton_amount: int = Column(Numeric)
    forward_payload = Column(String)

class JettonBurn(Base):
    __tablename__ = 'jetton_burns'
    transaction_hash = Column(String, primary_key=True)
    query_id: int = Column(Numeric)
    amount: int = Column(Numeric)
    response_destination = Column(String)
    custom_payload = Column(String)

class NFTCollection(Base):
    __tablename__ = 'nft_collections'
    address = Column(String, primary_key=True)
    next_item_index: int = Column(Numeric)
    owner_address = Column(String)
    collection_content = Column(JSONB)
    data_hash = Column(String)
    code_hash = Column(String)
    last_transaction_lt = Column(BigInteger)
    code_boc = Column(String)
    data_boc = Column(String)

class NFTItem(Base):
    __tablename__ = 'nft_items'
    address = Column(String, primary_key=True)
    init: bool = Column(Boolean)
    index: int = Column(Numeric)
    collection_address = Column(String)
    owner_address = Column(String)
    content = Column(JSONB)
    last_transaction_lt = Column(BigInteger)
    code_hash = Column(String)
    data_hash = Column(String)

class NFTTransfer(Base):
    __tablename__ = 'nft_transfers'
    transaction_hash = Column(String, primary_key=True)
    query_id: int = Column(Numeric)
    nft_item = Column(String)
    old_owner = Column(String)
    new_owner = Column(String)
    response_destination = Column(String)
    custom_payload = Column(String)
    forward_amount: int = Column(Numeric)
    forward_payload = Column(String)
