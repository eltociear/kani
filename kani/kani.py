import asyncio
import inspect
from typing import AsyncIterable, Callable

import cachetools
from pydantic import validate_call

from .ai_function import AIFunction
from .engines import BaseEngine
from .exceptions import NoSuchFunction, WrappedCallException, FunctionCallException
from .models import ChatMessage, FunctionCall, ChatRole


class Kani:
    """Base class for all kani.

    **Entrypoints**
    ``chat_round(query: str, **kwargs) -> str``
    ``full_round(query: str, function_call_formatter: Callable[[ChatMessage], str], **kwargs) -> AsyncIterable[str]``

    **Function Calling**
    Subclass and use ``@ai_function()`` to register functions. The schema will be autogenerated from the function
    signature (see :func:`ai_function`).

    To perform a chat round with functions, use ``full_round()`` as an async iterator::

        async for msg in kani.full_round(prompt):
            # responses...

    Each response will be a str; you can control the format of a yielded function call with ``function_call_formatter``.

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
        :param system_prompt: The system prompt to provide to the LM.
        :param always_include_messages: A list of messages to always include as a prefix in all chat rounds (i.e.,
            evict newer messages rather than these to manage context length).
        :param desired_response_tokens: The minimum amount of space to leave in ``max context size - tokens in prompt``.
        :param chat_history: The current chat history (None to start a new conversation). Will not include system
            messages or always_include_messages.
        :param functions: A list of :cls:`AIFunction` to expose to the model (if additional static @ai_function are
            defined, merges the two).
        :param retry_attempts: How many attempts the LM may take if a function call raises an exception.
        """
        self.engine = engine
        self.system_prompt = system_prompt.strip() if system_prompt else None
        self.desired_response_tokens = desired_response_tokens
        self.max_context_size = engine.max_context_size
        self.always_include_messages = ([ChatMessage.system(self.system_prompt)] if system_prompt else []) + (
            always_include_messages or []
        )
        self.chat_history: list[ChatMessage] = chat_history or []

        # async to prevent generating multiple responses missing context
        self.lock = asyncio.Lock()

        # function calling
        self.retry_attempts = retry_attempts

        # find all registered ai_functions and save them
        if functions is None:
            functions = []
        self.functions = {f.name: f for f in functions}
        for name, member in inspect.getmembers(self, predicate=inspect.ismethod):
            if not getattr(member, "__ai_function__", None):
                continue
            inner = validate_call(member)
            f: AIFunction = AIFunction(inner, **member.__ai_function__)
            if f.name in self.functions:
                raise ValueError(f"AIFunction {f.name!r} is already registered!")
            self.functions[f.name] = f

        # cache
        self._oldest_idx = 0
        self._message_tokens = cachetools.FIFOCache(256)

    def message_token_len(self, message: ChatMessage):
        """Returns the number of tokens used by a given message."""
        try:
            return self._message_tokens[message]
        except KeyError:
            mlen = self.engine.message_len(message)
            self._message_tokens[message] = mlen
            return mlen

    # === main entrypoints ===
    async def chat_round(self, query: str, **kwargs) -> str:
        """Perform a single chat round (user -> model -> user, no functions allowed).

        :param query: The user's query
        :param kwargs: Additional arguments to pass to the model engine (e.g. hyperparameters)
        :returns: The model's reply
        """
        async with self.lock:
            # get the user's chat input
            self.chat_history.append(ChatMessage.user(query.strip()))

            # get the context
            messages = await self.get_truncated_chat_history()

            # get the model's output, save it to chat history
            completion = await self.engine.predict(messages=messages, **kwargs)

            message = completion.message
            self._message_tokens[message] = completion.completion_tokens or self.message_token_len(message)
            self.chat_history.append(message)
            return message.content

    async def full_round(
        self,
        query: str,
        function_call_formatter: Callable[[ChatMessage], str | None] = lambda _: None,
        **kwargs,
    ) -> AsyncIterable[str]:
        """Perform a full chat round (user -> model [-> function -> model -> ...] -> user).

        Yields each of the model's utterances as a str.

        Use this in an async for loop, like so::

            async for msg in kani.full_round("How's the weather?"):
                print(msg)

        :param query: The user's query
        :param function_call_formatter: A function that returns the message to yield when the model decides to call a
            function (or None to yield nothing)
        :param kwargs: Additional arguments to pass to the model engine (e.g. hyperparameters)
        """
        retry = 0
        is_model_turn = True
        async with self.lock:
            self.chat_history.append(ChatMessage.user(query.strip()))

            while is_model_turn:
                is_model_turn = False

                # do the model prediction
                messages = await self.get_truncated_chat_history()
                completion = await self.engine.predict(
                    messages=messages, functions=list(self.functions.values()), **kwargs
                )
                # bookkeeping
                message = completion.message
                self._message_tokens[message] = completion.completion_tokens or self.message_token_len(message)
                self.chat_history.append(message)
                if text := message.content:
                    yield text

                # if function call, do it and attempt retry if it's wrong
                if not message.function_call:
                    return

                if fn_msg := function_call_formatter(message):
                    yield fn_msg

                try:
                    is_model_turn = await self.do_function_call(message.function_call)
                except FunctionCallException as e:
                    should_retry = await self.handle_function_call_exception(message.function_call, e, retry)
                    # retry if we have retry attempts left
                    retry += 1
                    if not should_retry:
                        # disable function calling on the next go
                        kwargs = {**kwargs, "function_call": "none"}
                    continue
                else:
                    retry = 0

    # ==== overridable methods ====
    async def get_truncated_chat_history(self) -> list[ChatMessage]:
        """
        Returns a list of messages such that the total token count in the messages is less than
        (max_context_size - desired_response_tokens).
        Always includes the system prompt plus any always_include_messages.

        You may override this to get more fine-grained control over what is exposed in the model's memory at any given
        call.
        """
        reversed_history = []
        always_len = sum(self.message_token_len(m) for m in self.always_include_messages) + self.engine.token_reserve
        remaining = self.max_context_size - (always_len + self.desired_response_tokens)
        for idx in range(len(self.chat_history) - 1, self._oldest_idx - 1, -1):
            message = self.chat_history[idx]
            message_len = self.message_token_len(message)
            remaining -= message_len
            if remaining > 0:
                reversed_history.append(message)
            else:
                self._oldest_idx = idx + 1
                break
        return self.always_include_messages + reversed_history[::-1]

    async def do_function_call(self, call: FunctionCall) -> bool:
        """Resolve a single function call.

        You may implement an override to add instrumentation around function calls (e.g. tracking success counts
        for varying prompts).

        By default, any exception raised from this method will be an instance of a :cls:`FunctionCallException`.

        :returns: True (default) if the model should immediately react; False if the user speaks next.
        """
        # get func
        f = self.functions.get(call.name)
        if not f:
            raise NoSuchFunction(call.name)
        # call it
        try:
            result = await f(**call.kwargs)
        except Exception as e:
            raise WrappedCallException(f.auto_retry, e) from e
        # save the result to the chat history
        self.chat_history.append(ChatMessage.function(f.name, str(result)))
        # yield whose turn it is
        return f.after == ChatRole.ASSISTANT

    async def handle_function_call_exception(
        self, call: FunctionCall, err: FunctionCallException, attempt: int
    ) -> bool:
        """Called when a function call raises an exception.

        By default, this adds a message to the chat telling the LM about the error and allows a retry if the error
        is recoverable and there are remaining retry attempts.

        You may implement an override to customize the error prompt, log the error, or use custom retry logic.

        :param call: The :cls:`FunctionCall` the model was attempting to make.
        :param err: The error the call raised. Usually this is :cls:`NoSuchFunction` or :cls:`WrappedCallException`,
            although it may be any exception raised by :meth:`do_function_call`.
        :param attempt: The attempt number for the current call (1-indexed).
        :returns: True if the model should retry the call; False if not.
        """
        # tell the model what went wrong
        if isinstance(err, NoSuchFunction):
            self.chat_history.append(
                ChatMessage.system(f"The function {err.name!r} is not defined. Only use the provided functions.")
            )
        else:
            self.chat_history.append(ChatMessage.function(call.name, str(err)))

        return attempt <= self.retry_attempts and err.retry