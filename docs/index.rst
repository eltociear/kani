kani (カニ)
===========

kani (カニ) is a lightweight and highly hackable framework for chat-based language models with tool usage/function
calling.

Compared to other LM frameworks, kani is less opinionated and offers more fine-grained customizability
over the parts of the control flow that matter, making it the perfect choice for NLP researchers, hobbyists, and
developers alike.

kani comes with support for OpenAI models and LLaMA v2 out of the box, with a model-agnostic framework to add support
for many more.

.. todo information about the paper and citations

Features
--------

- **Lightweight and high-level** - kani implements common boilerplate to interface with language models without forcing
  you to use opinionated prompt frameworks or complex library-specific tooling.
- **Model agnostic** - kani provides a simple interface to implement: token counting and completion generation.
  Implement these two, and kani can run with any language model.
- **Automatic chat memory management** - Allow chat sessions to flow without worrying about managing the number of
  tokens in the history - kani takes care of it.
- **Function calling with model feedback and retry** - Give models access to functions in just one line of code.
  kani elegantly provides feedback about hallucinated parameters and errors and allows the model to retry calls.
- **You control the prompts** - There are no hidden prompt hacks. We will never decide for you how to format your own
  data, unlike other popular language model libraries.
- **Fast to iterate and intuitive to learn** - With kani, you only write Python - we handle the rest.
- **Asynchronous design from the start** - kani can scale to run multiple chat sessions in parallel easily, without
  having to manage multiple processes or programs.

Quickstart
----------
kani requires Python 3.10 or above.

First, install the library. In this quickstart, we'll use the OpenAI engine, though kani is model-agnostic.

.. code-block:: console

    $ pip install "kani[openai]"

Then, let's use kani to create a simple chatbot using ChatGPT as a backend.

.. code-block:: python

    # import the library
    from kani import Kani, chat_in_terminal
    from kani.engines.openai import OpenAIEngine

    # Replace this with your OpenAI API key: https://platform.openai.com/account/api-keys
    api_key = "sk-..."

    # kani uses an Engine to interact with the language model. You can specify other model
    # parameters here, like temperature=0.7.
    engine = OpenAIEngine(api_key, model="gpt-3.5-turbo")

    # The kani manages the chat state, prompting, and function calling. Here, we only give
    # it the engine to call ChatGPT, but you can specify other parameters like system_prompt="You are..." here.
    ai = Kani(engine)

    # kani comes with a utility to interact with a kani through your terminal! Check out
    # the docs for how to use kani programmatically.
    chat_in_terminal(ai)

kani makes the time to set up a working chat model short, while offering the programmer deep customizability over
every prompt, function call, and even the underlying language model.

To learn more about how to customize kani with your own prompt wrappers, function calling, and more, read on!

Hands-on examples are available in the `kani repository <https://github.com/zhudotexe/kani/tree/main/examples>`_.

.. toctree::
    :maxdepth: 2
    :caption: Pages

    install
    kani
    function_calling
    customization
    engines
    advanced
    api_reference
    engine_reference
    contributing
    genindex
