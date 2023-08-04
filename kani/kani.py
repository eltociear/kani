import asyncio
import inspect
import logging
import weakref
from typing import AsyncIterable, Callable

from .ai_function import AIFunction
from .engines.base import BaseEngine, BaseCompletion
from .exceptions import NoSuchFunction, WrappedCallException, FunctionCallException, MessageTooLong
from .models import ChatMessage, FunctionCall, ChatRole
from .utils.typing import PathLike, SavedKani

log = logging.getLogger("kani")
message_log = logging.getLogger("kani.messages")


class Kani:
    """Base class for all kani.

    **Entrypoints**

    ``chat_round(query: str, **kwargs) -> ChatMessage``

    ``full_round(query: str, function_call_formatter: Callable[[ChatMessage], str], **kwargs) -> AsyncIterable[ChatMessage]``

    ``chat_round_str(query: str, **kwargs) -> str``

    ``full_round_str(query: str, function_call_formatter: Callable[[ChatMessage], str], **kwargs) -> AsyncIterable[str]``

    **Function Calling**

    Subclass and use ``@ai_function()`` to register functions. The schema will be autogenerated from the function
    signature (see :func:`ai_function`).

    To perform a chat round with functions, use :meth:`full_round()` as an async iterator::

        async for msg in kani.full_round(prompt):
            # responses...

    Each response will be a :class:`.ChatMessage`.

    Alternatively, you can use :meth:`full_round_str` and control the format of a yielded function call with
    ``function_call_formatter``.

    **Retry & Model Feedback**

    If the model makes an error when attempting to call a function (e.g. calling a function that does not exist or
    passing params with incorrect and non-coercible types) or the function raises an exception, Kani will send the
    error in a system message to the model, allowing it up to *retry_attempts* to correct itself and retry the call.
    """

    def __init__(
        self,
        engine: BaseEngine,
        system_prompt: str = None,
        always_include_messages: list[ChatMessage] = None,
        desired_response_tokens: int = 450,
        chat_history: list[ChatMessage] = None,
        functions: list[AIFunction] = None,
        retry_attempts: int = 1,
    ):
        """
        :param engine: The LM engine implementation to use.
        :param system_prompt: The system prompt to provide to the LM. The prompt will not be included in
            :attr:`chat_history`.
        :param always_include_messages: A list of messages to always include as a prefix in all chat rounds (i.e.,
            evict newer messages rather than these to manage context length). These will not be included in
            :attr:`chat_history`.
        :param desired_response_tokens: The minimum amount of space to leave in ``max context size - tokens in prompt``.
            To control the maximum number of tokens generated more precisely, you may be able to configure the engine
            (e.g. ``OpenAIEngine(..., max_tokens=250)``).
        :param chat_history: The chat history to start with (not including system prompt or always include messages),
            for advanced use cases. By default, each kani starts with a new conversation session.

            .. caution::
                If you pass another kani's chat history here without copying it, the same list will be mutated!
                Use ``chat_history=mykani.chat_history.copy()`` to pass a copy.
        :param functions: A list of :class:`.AIFunction` to expose to the model (for dynamic function calling).
            Use :func:`.ai_function` to define static functions (see :doc:`function_calling`).
        :param retry_attempts: How many attempts the LM may take if a function call raises an exception.
        """
        self.engine = engine
        self.system_prompt = system_prompt.strip() if system_prompt else None
        self.desired_response_tokens = desired_response_tokens
        self.max_context_size = engine.max_context_size

        self.always_include_messages: list[ChatMessage] = (
            [ChatMessage.system(self.system_prompt)] if system_prompt else []
        ) + (always_include_messages or [])
        """Chat messages that are always included as a prefix in the model's prompt.
        Includes the system message, if supplied."""

        self.chat_history: list[ChatMessage] = chat_history or []
        """All messages in the current chat state, not including system or always include messages."""

        # async to prevent generating multiple responses missing context
        self.lock = asyncio.Lock()

        # function calling
        self.retry_attempts = retry_attempts

        # find all registered ai_functions and save them
        if functions is None:
            functions = []
        self.functions: dict[str, AIFunction] = {f.name: f for f in functions}
        for name, member in inspect.getmembers(self, predicate=inspect.ismethod):
            if not hasattr(member, "__ai_function__"):
                continue
            f: AIFunction = AIFunction(member, **member.__ai_function__)
            if f.name in self.functions:
                raise ValueError(f"AIFunction {f.name!r} is already registered!")
            self.functions[f.name] = f

        # cache
        self._message_tokens = weakref.WeakKeyDictionary()

    # === main entrypoints ===
    async def chat_round(self, query: str, **kwargs) -> ChatMessage:
        """Perform a single chat round (user -> model -> user, no functions allowed).

        This is slightly faster when you are chatting with a kani with no AI functions defined.

        :param query: The contents of the user's chat message.
        :param kwargs: Additional arguments to pass to the model engine (e.g. hyperparameters).
        :returns: The model's reply.
        """
        async with self.lock:
            # add the user's chat input to the state
            self.chat_history.append(ChatMessage.user(query.strip()))

            # and get a completion
            completion = await self.get_model_completion(include_functions=False, **kwargs)
            message = completion.message
            self.chat_history.append(message)
            return message

    async def chat_round_str(self, query: str, **kwargs) -> str:
        """Like :meth:`chat_round`, but only returns the content of the message."""
        msg = await self.chat_round(query, **kwargs)
        return msg.content

    async def full_round(self, query: str, **kwargs) -> AsyncIterable[ChatMessage]:
        """Perform a full chat round (user -> model [-> function -> model -> ...] -> user).

        Yields each of the model's ChatMessages. A ChatMessage must have at least one of (content, function_call).

        Use this in an async for loop, like so::

            async for msg in kani.full_round("How's the weather?"):
                print(msg.content)

        :param query: The content of the user's chat message.
        :param kwargs: Additional arguments to pass to the model engine (e.g. hyperparameters).
        """
        retry = 0
        is_model_turn = True
        async with self.lock:
            self.chat_history.append(ChatMessage.user(query.strip()))

            while is_model_turn:
                # do the model prediction
                completion = await self.get_model_completion(**kwargs)
                message = completion.message
                self.chat_history.append(message)
                yield message

                # if function call, do it and attempt retry if it's wrong
                if not message.function_call:
                    return

                try:
                    is_model_turn = await self.do_function_call(message.function_call)
                except FunctionCallException as e:
                    should_retry = await self.handle_function_call_exception(message.function_call, e, retry)
                    # retry if we have retry attempts left
                    retry += 1
                    if not should_retry:
                        # disable function calling on the next go
                        kwargs = {**kwargs, "include_functions": False}
                    continue
                else:
                    retry = 0

    async def full_round_str(
        self,
        query: str,
        function_call_formatter: Callable[[ChatMessage], str | None] = lambda _: None,
        **kwargs,
    ) -> AsyncIterable[str]:
        """Like :meth:`full_round`, but each yielded element is a str rather than a ChatMessage.

        :param query: The content of the user's chat message.
        :param function_call_formatter: A function that returns a string to yield when the model decides to call a
            function (or None to yield nothing). By default, ``full_round_str`` does not yield on a function call.
        :param kwargs: Additional arguments to pass to the model engine (e.g. hyperparameters).
        """
        async for message in self.full_round(query, **kwargs):
            if text := message.content:
                yield text

            if message.function_call and (fn_msg := function_call_formatter(message)):
                yield fn_msg

    # ==== helpers ====
    @property
    def always_len(self) -> int:
        """Returns the number of tokens that will always be reserved.

        (e.g. for system prompts, always include messages, the engine, and the response).
        """
        return (
            sum(self.message_token_len(m) for m in self.always_include_messages)
            + self.engine.token_reserve
            + self.desired_response_tokens
        )

    def message_token_len(self, message: ChatMessage):
        """Returns the number of tokens used by a given message."""
        try:
            return self._message_tokens[message]
        except KeyError:
            mlen = self.engine.message_len(message)
            self._message_tokens[message] = mlen
            return mlen

    async def get_model_completion(self, include_functions: bool = True, **kwargs) -> BaseCompletion:
        """Get the model's completion with the current chat state.

        Compared to :meth:`chat_round` and :meth:`full_round`, this lower-level method does not save the model's reply
        to the chat history or mutate the chat state; it is intended to help with logging or to repeat a call multiple
        times.

        :param include_functions: Whether to pass this kani's function definitions to the engine.
        :param kwargs: Arguments to pass to the model engine.
        """
        # get the current chat state
        messages = await self.get_prompt()
        # log it (message_log includes the number of messages sent and the last message)
        n_messages = len(messages)
        if n_messages == 0:
            message_log.debug("[0]>>> [requested completion with no prompt]")
        else:
            message_log.debug(f"[{n_messages}]>>> {messages[-1]}")

        # get the model's completion at the given state
        if include_functions:
            completion = await self.engine.predict(messages=messages, functions=list(self.functions.values()), **kwargs)
        else:
            completion = await self.engine.predict(messages=messages, **kwargs)

        # cache its length (if the completion isn't saved to state, this weakrefs and gc's later)
        message = completion.message
        self._message_tokens[message] = completion.completion_tokens or self.message_token_len(message)
        # and log it too
        message_log.debug(f"<<< {message}")
        return completion

    # ==== overridable methods ====
    async def get_prompt(self) -> list[ChatMessage]:
        """
        Called each time before asking the LM engine for a completion to generate the chat prompt.
        Returns a list of messages such that the total token count in the messages is less than
        ``(self.max_context_size - self.desired_response_tokens)``.

        Always includes the system prompt plus any always_include_messages at the start of the prompt.

        You may override this to get more fine-grained control over what is exposed in the model's memory at any given
        call.
        """
        reversed_history = []
        always_len = self.always_len
        remaining = max_size = self.max_context_size - always_len
        total_tokens = 0
        for idx in range(len(self.chat_history) - 1, -1, -1):
            # get and check the message's length
            message = self.chat_history[idx]
            message_len = self.message_token_len(message)
            if message_len > max_size:
                func_help = (
                    ""
                    if message.role != ChatRole.FUNCTION
                    else "You may set `auto_truncate` in the @ai_function to automatically truncate long responses.\n"
                )
                raise MessageTooLong(
                    "The chat message's size is longer than the allowed context window (after including system"
                    " messages, always include messages, and desired response tokens).\n"
                    f"{func_help}Content: {message.content[:100]}..."
                )
            # see if we can include it
            remaining -= message_len
            if remaining >= 0:
                total_tokens += message_len
                reversed_history.append(message)
            else:
                break
        log.debug(
            f"get_prompt() returned {always_len + total_tokens} tokens ({always_len} always) in"
            f" {len(self.always_include_messages) + len(reversed_history)} messages"
            f" ({len(self.always_include_messages)} always)"
        )
        return self.always_include_messages + reversed_history[::-1]

    async def do_function_call(self, call: FunctionCall) -> bool:
        """Resolve a single function call.

        By default, any exception raised from this method will be an instance of a :class:`.FunctionCallException`.

        You may implement an override to add instrumentation around function calls (e.g. tracking success counts
        for varying prompts). See :ref:`do_function_call`.

        :returns: True (default) if the model should immediately react; False if the user speaks next.
        :raises NoSuchFunction: The requested function does not exist.
        :raises WrappedCallException: The function raised an exception.
        """
        log.debug(f"Model requested call to {call.name} with data: {call.arguments!r}")
        # get func
        f = self.functions.get(call.name)
        if not f:
            raise NoSuchFunction(call.name)
        # call it
        try:
            result = await f(**call.kwargs)
            result_str = str(result)
            log.debug(f"{f.name} responded with data: {result_str!r}")
        except Exception as e:
            raise WrappedCallException(f.auto_retry, e) from e
        msg = ChatMessage.function(f.name, result_str)
        # if we are auto truncating, check and see if we need to
        if f.auto_truncate is not None:
            message_len = self.message_token_len(msg)
            if message_len > f.auto_truncate:
                log.warning(
                    f"The content returned by {f.name} is too long ({message_len} > {f.auto_truncate} tokens), auto"
                    " truncating..."
                )
                msg = self._auto_truncate_message(msg, max_len=f.auto_truncate)
                log.debug(f"Auto truncate returned {self.message_token_len(msg)} tokens.")
        # save the result to the chat history
        self.chat_history.append(msg)
        # yield whose turn it is
        return f.after == ChatRole.ASSISTANT

    async def handle_function_call_exception(
        self, call: FunctionCall, err: FunctionCallException, attempt: int
    ) -> bool:
        """Called when a function call raises an exception.

        By default, this adds a message to the chat telling the LM about the error and allows a retry if the error
        is recoverable and there are remaining retry attempts.

        You may implement an override to customize the error prompt, log the error, or use custom retry logic.
        See :ref:`handle_function_call_exception`.

        :param call: The :class:`.FunctionCall` the model was attempting to make.
        :param err: The error the call raised. Usually this is :class:`.NoSuchFunction` or
            :class:`.WrappedCallException`, although it may be any exception raised by :meth:`do_function_call`.
        :param attempt: The attempt number for the current call (0-indexed).
        :returns: True if the model should retry the call; False if not.
        """
        # log the exception here
        log.debug(f"Call to {call.name} raised an exception: {err}")
        # tell the model what went wrong
        if isinstance(err, NoSuchFunction):
            self.chat_history.append(
                ChatMessage.system(f"The function {err.name!r} is not defined. Only use the provided functions.")
            )
        else:
            # but if it's a user function error, we want to raise it
            log.error(f"Call to {call.name} raised an exception: {err}", exc_info=err)
            self.chat_history.append(ChatMessage.function(call.name, str(err)))

        return attempt < self.retry_attempts and err.retry

    # ==== utility methods ====
    def save(self, fp: PathLike, **kwargs):
        """Save the chat state of this kani to a JSON file. This will overwrite the file if it exists!

        :param fp: The path to the file to save.
        :param kwargs: Additional arguments to pass to Pydantic's ``model_dump_json``.
        """
        data = SavedKani(always_include_messages=self.always_include_messages, chat_history=self.chat_history)
        with open(fp, "w") as f:
            f.write(data.model_dump_json(**kwargs))

    def load(self, fp: PathLike, **kwargs):
        """Load chat state from a JSON file into this kani. This will overwrite any existing chat state!

        :param fp: The path to the file containing the chat state.
        :param kwargs: Additional arguments to pass to Pydantic's ``model_validate_json``.
        """
        with open(fp) as f:
            data = f.read()
        state = SavedKani.model_validate_json(data, **kwargs)
        self.always_include_messages = state.always_include_messages
        self.chat_history = state.chat_history

    # ==== internals ====
    def _auto_truncate_message(self, msg: ChatMessage, max_len: int) -> ChatMessage:
        """Mutate the provided message until it is less than *max_len* tokens long."""
        full_content = msg.content
        if not full_content:
            return msg  # idk how this could happen
        for chunk_divider in ("\n\n", "\n", ". ", ", ", " "):
            # chunk the text
            content = ""
            last_msg = None
            chunks = full_content.split(chunk_divider)
            for idx, chunk in enumerate(chunks):
                # fit in as many chunks as possible
                if idx:
                    content += chunk_divider
                content += chunk
                # when it's too long...
                msg = ChatMessage(role=msg.role, name=msg.name, content=content + "...")
                if self.message_token_len(msg) > max_len:
                    # if we have some text, return it
                    if last_msg:
                        return last_msg
                    # otherwise, we need to split into smaller chunks
                    break
                # otherwise, continue
                last_msg = msg
        # if we get here and have no content, chop it to the first max_len characters
        log.warning(
            "Auto truncate could not find an appropriate place to chunk the text. The returned value will be the first"
            f" {max_len} characters."
        )
        return ChatMessage(role=msg.role, name=msg.name, content=full_content[: max_len - 3] + "...")
