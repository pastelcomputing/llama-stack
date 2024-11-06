# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the terms described in the LICENSE file in
# the root directory of this source tree.
from typing import AsyncGenerator

from llama_models.llama3.api.chat_format import ChatFormat
from llama_models.llama3.api.datatypes import Message
from llama_models.llama3.api.tokenizer import Tokenizer
from llama_models.sku_list import all_registered_models, resolve_model

from openai import OpenAI

from llama_stack.apis.inference import *  # noqa: F403
from llama_stack.providers.datatypes import ModelsProtocolPrivate

from llama_stack.providers.utils.inference.openai_compat import (
    get_sampling_options,
    process_chat_completion_response,
    process_chat_completion_stream_response,
)
from llama_stack.providers.utils.inference.prompt_adapter import (
    chat_completion_request_to_prompt,
)

from .config import VLLMInferenceAdapterConfig


class VLLMInferenceAdapter(Inference, ModelsProtocolPrivate):
    def __init__(self, config: VLLMInferenceAdapterConfig) -> None:
        self.config = config
        self.formatter = ChatFormat(Tokenizer.get_instance())
        self.client = None
        self.huggingface_repo_to_llama_model_id = {
            model.huggingface_repo: model.descriptor()
            for model in all_registered_models()
            if model.huggingface_repo
        }

    async def initialize(self) -> None:
        self.client = OpenAI(base_url=self.config.url, api_key=self.config.api_token)

    async def register_model(self, model: ModelDef) -> None:
        raise ValueError("Model registration is not supported for vLLM models")

    async def shutdown(self) -> None:
        pass

    async def list_models(self) -> List[ModelDef]:
        models = []
        for model in self.client.models.list():
            repo = model.id
            if repo not in self.huggingface_repo_to_llama_model_id:
                print(f"Unknown model served by vllm: {repo}")
                continue

            identifier = self.huggingface_repo_to_llama_model_id[repo]
            models.append(
                ModelDef(
                    identifier=identifier,
                    llama_model=identifier,
                )
            )
        return models

    async def completion(
        self,
        model: str,
        content: InterleavedTextMedia,
        sampling_params: Optional[SamplingParams] = SamplingParams(),
        response_format: Optional[ResponseFormat] = None,
        stream: Optional[bool] = False,
        logprobs: Optional[LogProbConfig] = None,
    ) -> Union[CompletionResponse, CompletionResponseStreamChunk]:
        raise NotImplementedError()

    async def chat_completion(
        self,
        model: str,
        messages: List[Message],
        sampling_params: Optional[SamplingParams] = SamplingParams(),
        response_format: Optional[ResponseFormat] = None,
        tools: Optional[List[ToolDefinition]] = None,
        tool_choice: Optional[ToolChoice] = ToolChoice.auto,
        tool_prompt_format: Optional[ToolPromptFormat] = ToolPromptFormat.json,
        stream: Optional[bool] = False,
        logprobs: Optional[LogProbConfig] = None,
    ) -> AsyncGenerator:
        request = ChatCompletionRequest(
            model=model,
            messages=messages,
            sampling_params=sampling_params,
            tools=tools or [],
            tool_choice=tool_choice,
            tool_prompt_format=tool_prompt_format,
            stream=stream,
            logprobs=logprobs,
        )
        if stream:
            return self._stream_chat_completion(request, self.client)
        else:
            return await self._nonstream_chat_completion(request, self.client)

    async def _nonstream_chat_completion(
        self, request: ChatCompletionRequest, client: OpenAI
    ) -> ChatCompletionResponse:
        params = self._get_params(request)
        r = client.completions.create(**params)
        return process_chat_completion_response(r, self.formatter)

    async def _stream_chat_completion(
        self, request: ChatCompletionRequest, client: OpenAI
    ) -> AsyncGenerator:
        params = self._get_params(request)

        # TODO: Can we use client.completions.acreate() or maybe there is another way to directly create an async
        #  generator so this wrapper is not necessary?
        async def _to_async_generator():
            s = client.completions.create(**params)
            for chunk in s:
                yield chunk

        stream = _to_async_generator()
        async for chunk in process_chat_completion_stream_response(
            stream, self.formatter
        ):
            yield chunk

    def _get_params(self, request: ChatCompletionRequest) -> dict:
        options = get_sampling_options(request.sampling_params)
        if "max_tokens" not in options:
            options["max_tokens"] = self.config.max_tokens

        model = resolve_model(request.model)
        if model is None:
            raise ValueError(f"Unknown model: {request.model}")

        return {
            "model": model.huggingface_repo,
            "prompt": chat_completion_request_to_prompt(request, self.formatter),
            "stream": request.stream,
            **options,
        }

    async def embeddings(
        self,
        model: str,
        contents: List[InterleavedTextMedia],
    ) -> EmbeddingsResponse:
        raise NotImplementedError()
