import asyncio
import logging
import json
import uuid
from dataclasses import dataclass, field
from typing import Dict, Optional, List, Set, Any

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command, StateFilter, BaseFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

# --- Configuration ---
class Config:
    # IMPORTANT: Replace with your actual main bot token obtained from BotFather
    MAIN_BOT_TOKEN: str = "6945856267:AAHdWr7B-T3llPgMmRaIJAHaX5u4f6E1pWI"

    # IMPORTANT: Replace with your Telegram User ID(s). You can get your ID from @userinfobot.
    ADMIN_USER_IDS: Set[int] = {1382414440}

    # IMPORTANT: Replace with your main group chat ID.
    # Create a group, add your main bot as admin (with topic management permissions),
    # then forward a message from the group to @userinfobot to get its ID (starts with -100).
    MAIN_GROUP_ID: int = -1002553104013

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(name)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Data Models ---
@dataclass
class Order:
    """Represents a single taxi order."""
    id: str
    from_location: str
    to_location: str
    phone: str
    luggage: str  # e.g., "Yes", "No", "Small"
    time: str      # e.g., "Now", "14:30", "Tomorrow morning"
    comment: str   # Any additional comments
    passengers: int
    status: str = "pending"  # pending, accepted, rejected, completed
    driver_id: Optional[int] = None
    driver_username: Optional[str] = None
    clone_bot_token: str  # Token of the bot that received the order
    customer_chat_id: int # Chat ID of the customer who placed the order
    customer_message_id: Optional[int] = None # Message ID of the order confirmation for customer

@dataclass
class BotInstance:
    """Represents a registered bot (main or clone)."""
    token: str
    name: str
    is_main: bool = False
    active: bool = True

@dataclass
class Route:
    """Represents a taxi route and its linked Telegram topic."""
    name: str
    thread_id: Optional[int] = None # Topic ID in the main group

# --- In-Memory Storage ---
class InMemoryStorage:
    """
    A simple in-memory storage for bot data.
    In a production environment, this would be replaced by a persistent database (e.g., SQLite, PostgreSQL).
    """
    def __init__(self):
        self.bot_instances: Dict[str, BotInstance] = {} # Maps token to BotInstance
        self.routes: Dict[str, Route] = {}             # Maps route_name to Route
        self.orders: Dict[str, Order] = {}             # Maps order_id to Order
        self.admin_users: Set[int] = set(Config.ADMIN_USER_IDS)

        # Add the main bot to storage upon initialization
        self.add_bot_instance(BotInstance(token=Config.MAIN_BOT_TOKEN, name="Main Bot", is_main=True))
        logger.info("In-memory storage initialized with main bot.")

    def add_bot_instance(self, bot_instance: BotInstance):
        """Adds or updates a bot instance in storage."""
        self.bot_instances[bot_instance.token] = bot_instance
        logger.info(f"Bot instance '{bot_instance.name}' added/updated in storage.")

    def get_bot_instance(self, token: str) -> Optional[BotInstance]:
        """Retrieves a bot instance by its token."""
        return self.bot_instances.get(token)

    def get_all_bot_instances(self) -> List[BotInstance]:
        """Returns a list of all registered bot instances."""
        return list(self.bot_instances.values())

    def delete_bot_instance(self, token: str):
        """Deletes a bot instance by its token."""
        if token in self.bot_instances:
            del self.bot_instances[token]
            logger.info(f"Bot instance with token '{token}' deleted from storage.")

    def add_route(self, route: Route):
        """Adds a new route to storage."""
        self.routes[route.name] = route
        logger.info(f"Route '{route.name}' added to storage.")

    def get_route(self, name: str) -> Optional[Route]:
        """Retrieves a route by its name."""
        return self.routes.get(name)

    def get_all_routes(self) -> List[Route]:
        """Returns a list of all registered routes."""
        return list(self.routes.values())

    def delete_route(self, name: str):
        """Deletes a route by its name."""
        if name in self.routes:
            del self.routes[name]
            logger.info(f"Route '{name}' deleted from storage.")

    def add_order(self, order: Order):
        """Adds a new order to storage."""
        self.orders[order.id] = order
        logger.info(f"Order '{order.id}' added to storage.")

    def get_order(self, order_id: str) -> Optional[Order]:
        """Retrieves an order by its ID."""
        return self.orders.get(order_id)

    def update_order(self, order: Order):
        """Updates an existing order in storage."""
        self.orders[order.id] = order
        logger.info(f"Order '{order.id}' updated in storage.")

    def get_pending_orders(self) -> List[Order]:
        """Returns a list of all orders with 'pending' status."""
        return [order for order in self.orders.values() if order.status == "pending"]

# --- Custom Filters ---
class IsAdmin(BaseFilter):
    """Filter to check if the message sender is an admin."""
    def __init__(self, storage: InMemoryStorage):
        self.storage = storage

    async def __call__(self, message: Message) -> bool:
        return message.from_user.id in self.storage.admin_users

class IsMainBot(BaseFilter):
    """Filter to check if the update came from the main bot's token."""
    async def __call__(self, bot: Bot) -> bool:
        return bot.token == Config.MAIN_BOT_TOKEN

class IsCloneBot(BaseFilter):
    """Filter to check if the update came from a clone bot's token."""
    async def __call__(self, bot: Bot) -> bool:
        return bot.token != Config.MAIN_BOT_TOKEN

# --- FSM for Client Ordering (Clone Bots) ---
class OrderStates(StatesGroup):
    """States for the order placement Finite State Machine."""
    waiting_for_from = State()
    waiting_for_to = State()
    waiting_for_phone = State()
    waiting_for_luggage = State()
    waiting_for_time = State()
    waiting_for_comment = State()
    waiting_for_passengers = State()
    confirm_order = State()

# --- Main Bot Handlers (Admin Panel & Order Distribution) ---
async def admin_start(message: Message, bot: Bot, storage: InMemoryStorage):
    """Handles /start and /admin commands for the main bot's admin panel."""
    if not await IsMainBot()(bot):
        return # Ensure this handler only responds to the main bot's token

    text = (
        "<b>Admin Panel</b>\n\n"
        "<b>Clone Bots:</b>\n"
        "/add_clone_bot &lt;token&gt; &lt;name&gt; - Add a new clone bot token\n"
        "/list_clone_bots - List all registered clone bots\n"
        "/delete_clone_bot &lt;token&gt; - Delete a clone bot\n\n"
        "<b>Routes:</b>\n"
        "/add_route &lt;name&gt; - Add a new route (e.g., CityA-CityB)\n"
        "/list_routes - List all routes\n"
        "/link_route &lt;name&gt; &lt;thread_id&gt; - Link route to a topic ID in the main group\n"
        "/delete_route &lt;name&gt; - Delete a route\n\n"
        "<b>Monitoring:</b>\n"
        "/list_pending_orders - List all pending orders\n"
    )
    await message.answer(text, parse_mode=ParseMode.HTML)

async def add_clone_bot(message: Message, bot: Bot, storage: InMemoryStorage):
    """Admin command to add a new clone bot token."""
    if not await IsMainBot()(bot): return
    args = message.text.split(maxsplit=2)
    if len(args) != 3:
        await message.answer("Usage: /add_clone_bot <token> <name>")
        return
    token, name = args[1], args[2]
    if storage.get_bot_instance(token):
        await message.answer(f"Bot with token '{token}' already exists.")
        return
    storage.add_bot_instance(BotInstance(token=token, name=name, is_main=False, active=True))
    await message.answer(f"Clone bot '{name}' added successfully. <b>You need to restart the `taxi_bot.py` script for it to become active.</b>", parse_mode=ParseMode.HTML)

async def list_clone_bots(message: Message, bot: Bot, storage: InMemoryStorage):
    """Admin command to list all registered clone bots."""
    if not await IsMainBot()(bot): return
    bots = [b for b in storage.get_all_bot_instances() if not b.is_main]
    if not bots:
        await message.answer("No clone bots registered.")
        return
    text = "<b>Registered Clone Bots:</b>\n"
    for b in bots:
        text += f"- <code>{b.token[:5]}...</code> | <b>{b.name}</b> (Active: {b.active})\n"
    await message.answer(text, parse_mode=ParseMode.HTML)

async def delete_clone_bot(message: Message, bot: Bot, storage: InMemoryStorage):
    """Admin command to delete a clone bot by its token."""
    if not await IsMainBot()(bot): return
    args = message.text.split(maxsplit=1)
    if len(args) != 2:
        await message.answer("Usage: /delete_clone_bot <token>")
        return
    token = args[1]
    if not storage.get_bot_instance(token):
        await message.answer(f"Bot with token '{token}' not found.")
        return
    storage.delete_bot_instance(token)
    await message.answer(f"Clone bot with token '{token}' deleted. <b>You need to restart the `taxi_bot.py` script for it to be fully deactivated.</b>", parse_mode=ParseMode.HTML)

async def add_route(message: Message, bot: Bot, storage: InMemoryStorage):
    """Admin command to add a new route."""
    if not await IsMainBot()(bot): return
    args = message.text.split(maxsplit=1)
    if len(args) != 2:
        await message.answer("Usage: /add_route <name>")
        return
    name = args[1].strip()
    if storage.get_route(name):
        await message.answer(f"Route '{name}' already exists.")
        return
    storage.add_route(Route(name=name))
    await message.answer(f"Route '{name}' added successfully.")

async def list_routes(message: Message, bot: Bot, storage: InMemoryStorage):
    """Admin command to list all registered routes."""
    if not await IsMainBot()(bot): return
    routes = storage.get_all_routes()
    if not routes:
        await message.answer("No routes registered.")
        return
    text = "<b>Registered Routes:</b>\n"
    for r in routes:
        thread_info = f" (Topic ID: <code>{r.thread_id}</code>)" if r.thread_id else " (<b>NOT LINKED</b>)"
        text += f"- <b>{r.name}</b>{thread_info}\n"
    await message.answer(text, parse_mode=ParseMode.HTML)

async def link_route(message: Message, bot: Bot, storage: InMemoryStorage):
    """Admin command to link a route to a Telegram group topic ID."""
    if not await IsMainBot()(bot): return
    args = message.text.split(maxsplit=2)
    if len(args) != 3:
        await message.answer("Usage: /link_route <name> <thread_id>\n"
                             "Get thread_id by creating a topic in your main group, copying its link, and extracting the ID.")
        return
    name = args[1].strip()
    try:
        thread_id = int(args[2])
    except ValueError:
        await message.answer("Invalid thread_id. Must be an integer.")
        return

    route = storage.get_route(name)
    if not route:
        await message.answer(f"Route '{name}' not found. Please add it first using /add_route.")
        return
    route.thread_id = thread_id
    storage.add_route(route) # Use add_route to update existing entry
    await message.answer(f"Route '{name}' linked to topic ID <code>{thread_id}</code> successfully.", parse_mode=ParseMode.HTML)

async def delete_route(message: Message, bot: Bot, storage: InMemoryStorage):
    """Admin command to delete a route by its name."""
    if not await IsMainBot()(bot): return
    args = message.text.split(maxsplit=1)
    if len(args) != 2:
        await message.answer("Usage: /delete_route <name>")
        return
    name = args[1].strip()
    if not storage.get_route(name):
        await message.answer(f"Route '{name}' not found.")
        return
    storage.delete_route(name)
    await message.answer(f"Route '{name}' deleted.")

async def list_pending_orders(message: Message, bot: Bot, storage: InMemoryStorage):
    """Admin command to list all currently pending orders."""
    if not await IsMainBot()(bot): return
    orders = storage.get_pending_orders()
    if not orders:
        await message.answer("No pending orders.")
        return
    text = "<b>Pending Orders:</b>\n"
    for order in orders:
        clone_bot_info = storage.get_bot_instance(order.clone_bot_token)
        clone_bot_name = clone_bot_info.name if clone_bot_info else 'Unknown'
        text += (
            f"ID: <code>{order.id[:8]}...</code>\n"
            f"From: {order.from_location}\n"
            f"To: {order.to_location}\n"
            f"Phone: {order.phone}\n"
            f"Status: {order.status}\n"
            f"Clone Bot: {clone_bot_name}\n\n"
        )
    await message.answer(text, parse_mode=ParseMode.HTML)

# --- Order Distribution & Driver Interaction ---
def get_order_markup(order_id: str) -> InlineKeyboardMarkup:
    """Generates the inline keyboard for drivers to accept/reject an order."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Accept Order", callback_data=f"accept_order_{order_id}"),
            InlineKeyboardButton(text="❌ Reject Order", callback_data=f"reject_order_{order_id}")
        ]
    ])

async def distribute_order(main_bot: Bot, order: Order, storage: InMemoryStorage):
    """
    Distributes a new order to the appropriate Telegram group topic.
    This function is called by the clone bot's FSM handler after an order is confirmed.
    """
    # Construct route name from order details for lookup
    route_name_from_order = f"{order.from_location} → {order.to_location}"
    route = storage.get_route(route_name_from_order)

    target_thread_id = None
    if route and route.thread_id:
        target_thread_id = route.thread_id
        logger.info(f"Distributing order {order.id} to topic {target_thread_id} for route '{route.name}'.")
    else:
        logger.warning(f"No route or thread_id found for order {order.id} ({route_name_from_order}). Sending to main group without specific topic.")
        # If no specific topic is linked, send to the general group (thread_id=None)

    order_text = (
        f"<b>🚨 NEW ORDER ALERT 🚨</b>\n\n"
        f"<b>From:</b> {order.from_location}\n"
        f"<b>To:</b> {order.to_location}\n"
        f"<b>Phone:</b> <code>{order.phone}</code>\n"
        f"<b>Luggage:</b> {order.luggage}\n"
        f"<b>Time:</b> {order.time}\n"
        f"<b>Passengers:</b> {order.passengers}\n"
        f"<b>Comment:</b> {order.comment if order.comment else 'N/A'}\n\n"
        f"Order ID: <code>{order.id[:8]}...</code>"
    )

    try:
        # Send the order message to the main group/topic
        msg = await main_bot.send_message(
            chat_id=Config.MAIN_GROUP_ID,
            text=order_text,
            reply_markup=get_order_markup(order.id),
            parse_mode=ParseMode.HTML,
            message_thread_id=target_thread_id
        )
        logger.info(f"Order {order.id} successfully posted to group {Config.MAIN_GROUP_ID}, topic {target_thread_id}.")
    except Exception as e:
        logger.error(f"Failed to send order {order.id} to group/topic: {e}")
        # In a real system, you might want to notify an admin or log this failure more prominently.

async def handle_order_callback(query: CallbackQuery, main_bot: Bot, storage: InMemoryStorage):
    """Handles driver's 'Accept' or 'Reject' callback queries for orders."""
    # Parse callback data: e.g., "accept_order_xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
    action, order_id = query.data.split('_', 1)
    order = storage.get_order(order_id)

    if not order:
        await query.answer("This order was not found or has been removed.", show_alert=True)
        return

    if order.status != "pending":
        await query.answer(f"This order has already been {order.status} by {order.driver_username or 'another driver'}.", show_alert=True)
        return

    driver_id = query.from_user.id
    driver_username = query.from_user.username or query.from_user.full_name

    if action == "accept":
        order.status = "accepted"
        order.driver_id = driver_id
        order.driver_username = driver_username
        storage.update_order(order)
        await query.answer("You have accepted this order!", show_alert=True)

        # Edit the original message in the group topic to show it's accepted
        try:
            await main_bot.edit_message_text(
                chat_id=query.message.chat.id,
                message_id=query.message.message_id,
                text=(
                    f"{query.message.html_text}\n\n"
                    f"<b>Status: ✅ Accepted by @{driver_username}</b>"
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=None # Remove buttons after acceptance
            )
        except Exception as e:
            logger.error(f"Failed to edit order message {order.id} in group: {e}")

        # Notify the customer who placed the order
        if order.customer_chat_id:
            try:
                await main_bot.send_message(
                    chat_id=order.customer_chat_id,
                    text=(
                        f"🎉 Your order (ID: <code>{order.id[:8]}...</code>) from <b>{order.from_location}</b> to <b>{order.to_location}</b> "
                        f"has been <b>ACCEPTED</b> by <b>@{driver_username}</b>! "
                        f"Driver's Telegram ID: <code>{driver_id}</code>. Please contact them via Telegram for details."
                    ),
                    parse_mode=ParseMode.HTML,
                    reply_to_message_id=order.customer_message_id # Reply to their original confirmation
                )
                logger.info(f"Customer {order.customer_chat_id} notified about accepted order {order.id}.")
            except Exception as e:
                logger.error(f"Failed to notify customer {order.customer_chat_id} about accepted order {order.id}: {e}")

    elif action == "reject":
        order.status = "rejected" # Mark as rejected by this driver, but it could still be pending for others
        storage.update_order(order)
        await query.answer("You have rejected this order.", show_alert=True)

        # Edit the original message in the group topic to show it's rejected by this driver
        try:
            await main_bot.edit_message_text(
                chat_id=query.message.chat.id,
                message_id=query.message.message_id,
                text=(
                    f"{query.message.html_text}\n\n"
                    f"<b>Status: ❌ Rejected by @{driver_username}</b>"
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=None # Remove buttons after rejection by a driver
            )
        except Exception as e:
            logger.error(f"Failed to edit order message {order.id} in group: {e}")
        # Customer is generally not notified on rejection, as another driver might still accept it.

# --- Clone Bot Handlers (Customer FSM) ---
async def clone_bot_start(message: Message, state: FSMContext, bot: Bot):
    """Handles /start command for clone bots, initiating the order FSM."""
    if await IsMainBot()(bot):
        return # Main bot's /start is handled by admin_start

    await message.answer(
        "👋 Welcome! Let's place your taxi order.\n"
        "Please tell me your <b>pickup location</b> (e.g., 'CityA, Street 123').",
        parse_mode=ParseMode.HTML
    )
    await state.set_state(OrderStates.waiting_for_from)

async def process_from_location(message: Message, state: FSMContext):
    """Processes the 'from' location input."""
    if not message.text or not message.text.strip():
        await message.answer("Please provide a valid pickup location.")
        return
    await state.update_data(from_location=message.text.strip())
    await message.answer("Great! Now, what is your <b>destination</b> (e.g., 'CityB, Main Square')?", parse_mode=ParseMode.HTML)
    await state.set_state(OrderStates.waiting_for_to)

async def process_to_location(message: Message, state: FSMContext):
    """Processes the 'to' location input."""
    if not message.text or not message.text.strip():
        await message.answer("Please provide a valid destination.")
        return
    await state.update_data(to_location=message.text.strip())
    await message.answer("What is your <b>phone number</b> (e.g., '+1234567890')?", parse_mode=ParseMode.HTML)
    await state.set_state(OrderStates.waiting_for_phone)

async def process_phone(message: Message, state: FSMContext):
    """Processes the phone number input."""
    if not message.text or not message.text.strip():
        await message.answer("Please provide a valid phone number.")
        return
    await state.update_data(phone=message.text.strip())
    await message.answer("Do you have any <b>luggage</b>? (e.g., 'No', 'Small bag', 'Large suitcase')", parse_mode=ParseMode.HTML)
    await state.set_state(OrderStates.waiting_for_luggage)

async def process_luggage(message: Message, state: FSMContext):
    """Processes the luggage information input."""
    if not message.text or not message.text.strip():
        await message.answer("Please specify if you have luggage.")
        return
    await state.update_data(luggage=message.text.strip())
    await message.answer("When do you need the taxi? (e.g., 'Now', '15:30', 'Tomorrow morning')", parse_mode=ParseMode.HTML)
    await state.set_state(OrderStates.waiting_for_time)

async def process_time(message: Message, state: FSMContext):
    """Processes the time input."""
    if not message.text or not message.text.strip():
        await message.answer("Please specify the time.")
        return
    await state.update_data(time=message.text.strip())
    await message.answer("Any <b>additional comments</b> for the driver? (e.g., 'Meet at entrance', 'Call upon arrival', or 'None')", parse_mode=ParseMode.HTML)
    await state.set_state(OrderStates.waiting_for_comment)

async def process_comment(message: Message, state: FSMContext):
    """Processes the additional comments input."""
    await state.update_data(comment=message.text.strip() if message.text else "None")
    await message.answer("How many <b>passengers</b>? (e.g., '1', '2')", parse_mode=ParseMode.HTML)
    await state.set_state(OrderStates.waiting_for_passengers)

async def process_passengers(message: Message, state: FSMContext):
    """Processes the number of passengers input and presents order for confirmation."""
    try:
        passengers = int(message.text.strip())
        if passengers <= 0:
            raise ValueError
    except (ValueError, TypeError):
        await message.answer("Please enter a valid number of passengers (e.g., '1', '2').")
        return

    await state.update_data(passengers=passengers)
    user_data = await state.get_data()

    confirmation_text = (
        "<b>Please review your order details:</b>\n\n"
        f"<b>From:</b> {user_data.get('from_location')}\n"
        f"<b>To:</b> {user_data.get('to_location')}\n"
        f"<b>Phone:</b> <code>{user_data.get('phone')}</code>\n"
        f"<b>Luggage:</b> {user_data.get('luggage')}\n"
        f"<b>Time:</b> {user_data.get('time')}\n"
        f"<b>Passengers:</b> {user_data.get('passengers')}\n"
        f"<b>Comment:</b> {user_data.get('comment')}\n\n"
        "Is everything correct? Type 'yes' to confirm or 'no' to restart."
    )
    await message.answer(confirmation_text, parse_mode=ParseMode.HTML)
    await state.set_state(OrderStates.confirm_order)

async def confirm_order(message: Message, state: FSMContext, main_bot: Bot, storage: InMemoryStorage, current_bot: Bot):
    """Confirms the order and initiates its distribution."""
    if message.text.lower().strip() == 'yes':
        user_data = await state.get_data()
        order_id = str(uuid.uuid4()) # Generate a unique ID for the order

        order = Order(
            id=order_id,
            from_location=user_data['from_location'],
            to_location=user_data['to_location'],
            phone=user_data['phone'],
            luggage=user_data['luggage'],
            time=user_data['time'],
            comment=user_data['comment'],
            passengers=user_data['passengers'],
            clone_bot_token=current_bot.token, # Store which bot received the order
            customer_chat_id=message.chat.id
        )
        storage.add_order(order)

        # Notify customer that order is being processed
        customer_confirmation_msg = await message.answer(
            "✅ Your order has been received and is being processed! We will notify you once a driver accepts it."
        )
        # Update order with customer message ID for later editing/replying
        order.customer_message_id = customer_confirmation_msg.message_id
        storage.update_order(order) # Update the stored order with the message ID

        # Distribute order to main group topics using the main_bot instance
        await distribute_order(main_bot, order, storage)

        await state.clear() # Clear FSM state after successful order
    elif message.text.lower().strip() == 'no':
        await message.answer("Order cancelled. You can start a new one with /start.")
        await state.clear() # Clear FSM state
    else:
        await message.answer("Please type 'yes' or 'no'.")

# --- Main Function to Run Bots ---
async def main():
    """Initializes and starts polling for all registered bots."""
    storage = InMemoryStorage()
    # Initialize the main bot instance
    main_bot = Bot(token=Config.MAIN_BOT_TOKEN, parse_mode=ParseMode.HTML)
    # Initialize a shared Dispatcher for all bots. MemoryStorage for FSM states.
    dp = Dispatcher(storage=MemoryStorage())

    # Pass main_bot instance and storage to handlers via workflow_data.
    # This allows clone bot handlers to access the main_bot object for distribution.
    dp.workflow_data.update({"main_bot": main_bot, "storage": storage})

    # --- Register Admin Handlers (only for main bot token) ---
    dp.message.register(admin_start, CommandStart(), IsAdmin(storage), IsMainBot())
    dp.message.register(admin_start, Command("admin"), IsAdmin(storage), IsMainBot())
    dp.message.register(add_clone_bot, Command("add_clone_bot"), IsAdmin(storage), IsMainBot())
    dp.message.register(list_clone_bots, Command("list_clone_bots"), IsAdmin(storage), IsMainBot())
    dp.message.register(delete_clone_bot, Command("delete_clone_bot"), IsAdmin(storage), IsMainBot())
    dp.message.register(add_route, Command("add_route"), IsAdmin(storage), IsMainBot())
    dp.message.register(list_routes, Command("list_routes"), IsAdmin(storage), IsMainBot())
    dp.message.register(link_route, Command("link_route"), IsAdmin(storage), IsMainBot())
    dp.message.register(delete_route, Command("delete_route"), IsAdmin(storage), IsMainBot())
    dp.message.register(list_pending_orders, Command("list_pending_orders"), IsAdmin(storage), IsMainBot())

    # --- Register Order Callback Handlers (for main bot token, from group) ---
    # These handle driver interactions (accept/reject) in the main group.
    dp.callback_query.register(handle_order_callback, F.data.startswith("accept_order_"), IsMainBot())
    dp.callback_query.register(handle_order_callback, F.data.startswith("reject_order_"), IsMainBot())

    # --- Register Clone Bot FSM Handlers (for clone bot tokens) ---
    # These handlers are for customer interactions. They use IsCloneBot() filter.
    dp.message.register(clone_bot_start, CommandStart(), IsCloneBot())
    dp.message.register(process_from_location, OrderStates.waiting_for_from, IsCloneBot())
    dp.message.register(process_to_location, OrderStates.waiting_for_to, IsCloneBot())
    dp.message.register(process_phone, OrderStates.waiting_for_phone, IsCloneBot())
    dp.message.register(process_luggage, OrderStates.waiting_for_luggage, IsCloneBot())
    dp.message.register(process_time, OrderStates.waiting_for_time, IsCloneBot())
    dp.message.register(process_comment, OrderStates.waiting_for_comment, IsCloneBot())
    dp.message.register(process_passengers, OrderStates.waiting_for_passengers, IsCloneBot())
    dp.message.register(confirm_order, OrderStates.confirm_order, IsCloneBot())

    # Prepare list of bot instances to poll concurrently
    bots_to_poll: List[Bot] = []

    # Add the main bot to the list
    bots_to_poll.append(main_bot)
    logger.info(f"Main Bot '{main_bot.id}' (token: {main_bot.token[:5]}...) initialized.")

    # Dynamically initialize and add clone bots from storage
    for bot_instance in storage.get_all_bot_instances():
        if not bot_instance.is_main and bot_instance.active:
            try:
                # Create a Bot instance for each active clone bot token
                clone_bot = Bot(token=bot_instance.token, parse_mode=ParseMode.HTML)
                bots_to_poll.append(clone_bot)
                logger.info(f"Clone Bot '{bot_instance.name}' (token: {clone_bot.token[:5]}...) initialized.")
            except Exception as e:
                logger.error(f"Failed to initialize clone bot {bot_instance.name} (token: {bot_instance.token[:5]}...): {e}")

    logger.info(f"Starting polling for {len(bots_to_poll)} bot(s) concurrently...")
    # Start polling for all bot instances. The shared Dispatcher handles routing updates.
    await asyncio.gather(*(dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types()) for bot in bots_to_poll))

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot system stopped by user (KeyboardInterrupt).")
    except Exception as e:
        logger.exception("An unhandled error occurred during bot execution.")

