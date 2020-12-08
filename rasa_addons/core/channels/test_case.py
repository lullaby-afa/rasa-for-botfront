import asyncio
import rasa
import os
import logging
import inspect
from rasa.core.channels.channel import UserMessage, CollectingOutputChannel, InputChannel
from rasa.core.channels.rest import RestInput
from sanic.request import Request
from sanic import Sanic, Blueprint, response
from asyncio import Queue, CancelledError
from typing import Text, List, Dict, Any, Optional, Callable, Iterable, Awaitable
from rasa.core import utils
from sanic.response import HTTPResponse
from rasa_addons.core.channels.graphql import get_config_via_graphql
from rasa_addons.core.channels.rest import BotfrontRestOutput
from datetime import datetime
from rasa.shared.core.events import UserUttered
from sgqlc.endpoint.http import HTTPEndpoint
import urllib.error

logger = logging.getLogger(__name__)

class TestCaseOutput(BotfrontRestOutput):
    def name(csl) -> Text:
        return "botfront_test_output"
    
    @staticmethod
    def extract_entities(entities: List[Dict[Text, Any]]) -> List[Dict[Text, Any]]:
        formatted_entities = []
        for entity in entities:
            formatted_entity = {
                "entity": entity.get("entity"),
                "start": entity.get("start"),
                "end": entity.get("end"),
                "value": entity.get("value"),
            }
            formatted_entities.append(formatted_entity)
        return formatted_entities

    def send_parsed_message (
        self,
        message: UserMessage,
    ) -> None:
        formatted_message = {
            "user": message.text,
            "intent": message.intent.get("name"),
            "entities": self.extract_entities(message.entities),
        }
        self.messages.append(formatted_message)


class TestCaseInput(RestInput):
    def __init__(self, config: Optional[Dict[Text, Any]] = None):
        self.url = config.get("url")

    @classmethod
    def from_credentials(cls, credentials: Optional[Dict[Text, Any]]) -> InputChannel:
        credentials = credentials or {}
        return cls(credentials)

    def name(cls) -> Text:
       return "test_case"

    def _extract_test_cases(self, req: Request) -> Text:
        return req.json.get("test_cases")
    def _extract_project_id(self, req: Request) -> Text:
        return req.json.get("project_id")

    async def simulate_messages(self, steps: List[Dict[Text, Any]], language: Text, on_new_message: Callable[[UserMessage], Awaitable[None]]) -> List[Dict[Text, Any]]:
        sender_id = 'botfront_test_case_{:%Y-%m-%d_%H:%M:%S}'.format(datetime.now())
        collector = TestCaseOutput()
        for step in steps:
            if "user" in step:
                text = step.get("user")
                metadata = { "lang": language }
                try:
                    await on_new_message(
                        UserMessage(
                            text,
                            collector,
                            sender_id,
                            input_channel=self.name(),
                            metadata=metadata,
                        )
                    )
                except CancelledError:
                    logger.error(
                        "Message handling timed out for "
                        "user message '{}'.".format(text)
                    )
                except Exception:
                    logger.exception(
                        "An exception occured while handling "
                        "user message '{}'.".format(text)
                    )
        return collector

    @staticmethod
    def compare_one_entity(actual, expected) -> bool:
        return actual.get("entity") == expected.get("entity") and actual.get("value") == expected.get("value") and actual.get("start") == expected.get("start") and actual.get("end") == expected.get("end")

    def compare_entities(self, actual_entities, expected_entites) -> bool:
        expected_remaining = expected_entites.copy()
        for actual in actual_entities:
            index_of_match = next((i for i, expected in enumerate(expected_remaining) if self.compare_one_entity(actual, expected)), -1)
            if index_of_match in range(0, len(expected_remaining)):
                expected_remaining.pop(index_of_match)
        return len(expected_remaining) == 0 and len(actual_entities) == len(expected_entites)

    def compare_steps(self, actual_step, expected_step) -> bool:
        if "user" in actual_step and "user" in expected_step:
            return (actual_step.get("text") == expected_step.get("text")
            and actual_step.get("intent") == expected_step.get("intent")
            and self.compare_entities(actual_step.get("entities"), expected_step.get("entities")))
        elif "action" in actual_step and "action" in expected_step:
            return actual_step.get("action") == expected_step.get("action")
        else: return False
    
    @staticmethod
    def format_as_step(step: Dict[Text, Any]) -> Dict[Text, Any]:
        if "metadata" in step and "template_name" in step.get("metadata"):
            return { "action": step.get("metadata").get("template_name") }
        else: return step

    def get_index_of_step(
        self,
        step: Dict[Text, Any],
        step_list: List[Dict[Text, Any]],
    ) -> int:
        return next((
            i for i, current_step
            in enumerate(step_list)
            if self.compare_steps(step, current_step)
        ), None)

    @staticmethod
    def accumulate_actual_step(
        step: Dict[Text, Any],
        results_acc: Dict[Text, Any],
    ) -> Dict[Text, Any]:
        step_to_append = step.copy()
        step_to_append["theme"] = "actual"
        results_acc.get("actual").append(step_to_append)
    
    @staticmethod
    def accumulate_matching_step(
        step: Dict[Text, Any],
        results_acc: Dict[Text, Any],
    ) -> Dict[Text, Any]:
        results_acc.get("steps").extend(results_acc.get("expected"))
        results_acc["expected"] = []
        results_acc.get("steps").append(step)
    
    @staticmethod
    def accumulate_expected_steps(
        expected_steps: List[Dict[Text, Any]],
        stop_index: int,
        results_acc: Dict[Text, Any],
    ) -> Dict[Text, Any]:
        results_acc.get("steps").extend(results_acc.get("actual"))
        results_acc["actual"] = []
        for _ in range(0, stop_index):
            step_to_append = expected_steps.pop(0).copy()
            step_to_append["theme"] = "expected"
            results_acc.get("steps").append(step_to_append)

    def compare_step_lists(self, actual_steps, expected_steps) -> List[Dict[Text, Any]]:
        expected_steps_remaining = expected_steps.copy()
        results_acc = {
            "expected": [],
            "actual": [],
            "steps": [],
        }
        for i, unformatted_actual_step in enumerate(actual_steps):
            actual_step = self.format_as_step(unformatted_actual_step)
            match_index = self.get_index_of_step(actual_step, expected_steps_remaining)
            if match_index is None:
                self.accumulate_actual_step(actual_step, results_acc)
            elif match_index in range(0, len(expected_steps_remaining)):
                if match_index > 0:
                    self.accumulate_expected_steps(
                        expected_steps_remaining,
                        match_index,
                        results_acc
                    )
                matched_step = expected_steps_remaining.pop(0)
                self.accumulate_matching_step(matched_step, results_acc)
            print('step complete')
        # accumulate expected steps will add any leftover expected/actual steps to results_acc.steps
        self.accumulate_expected_steps(
            expected_steps_remaining,
            len(expected_steps_remaining),
            results_acc
        )
        return results_acc.get("steps")
    
    @staticmethod
    def check_success(steps: List[Dict[Text, Any]]) -> bool:
        return next((False for step in steps if "theme" in step), True)

    async def run_tests (self, test_cases: List[Dict[Text, Any]], project_id: Text, on_new_message: Callable[[UserMessage], Awaitable[None]]) -> List[Dict[Text, Any]]:
        all_results = []
        for test_case in test_cases:
            expected_steps = test_case.get("steps")
            collector = await self.simulate_messages(expected_steps, test_case.get("language"), on_new_message)
            test_results = self.compare_step_lists(collector.messages, test_case.get("steps"))
            all_results.append({
                "_id": test_case.get("_id"),
                "testResults": test_results,
                "success": self.check_success(test_results),
                "projectId": project_id,
            })
        return all_results
    
    def blueprint(
        self, on_new_message: Callable[[UserMessage], Awaitable[None]]
    ) -> Blueprint:
        custom_webhook = Blueprint(
            "custom_webhook_{}".format(type(self).__name__),
            inspect.getmodule(self).__name__,
        )

        # noinspection PyUnusedLocal
        @custom_webhook.route("/", methods=["GET"])
        async def health(request: Request) -> HTTPResponse:
            return response.json({"status": "ok"})
        
        @custom_webhook.route("/run", methods=["POST"])
        async def receive(request: Request) -> HTTPResponse:
            should_use_stream = rasa.utils.endpoints.bool_arg(
                request, "stream", default=False
            )
            test_cases = self._extract_test_cases(request)
            project_id = self._extract_project_id(request)
            response.json({ "success": True })
            results = await self.run_tests(test_cases, project_id, on_new_message)
            return response.json(results)

        return custom_webhook
