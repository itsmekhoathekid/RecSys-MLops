"""Read child tasks; never call SendMessage or replay an agent/tool.

Wire contract: kagent commit e6df917e9fa8 remote_a2a_tool.go supplies
subagent_session_id. Read APIs enforce the same user identity as the root call.
"""
import os
import sys
from pathlib import Path
import uuid


class ChildTasks:
    def __init__(self, principal, agent_ids=()):
        import grpc
        from google.protobuf.json_format import MessageToDict
        sys.path.insert(0, str(Path(__file__).resolve().parent / "_protocol"))
        from kagent.api.v1alpha1 import sessions_pb2 as pb, sessions_pb2_grpc as rpc
        self.pb, self.to_dict = pb, MessageToDict
        self.metadata = (("x-user-id", principal),)
        self.agent_ids = frozenset(agent_ids)
        # Operator-owned target only; neither Langfuse nor a model can supply it.
        target = os.environ["AB_KAGENT_GRPC_TARGET"]
        self.channel = grpc.insecure_channel(target, options=[("grpc.enable_retries", 0),
                                             ("grpc.max_receive_message_length", 8 * 1024 * 1024)])
        self.sessions = rpc.SessionServiceStub(self.channel)
        self.tasks = rpc.TaskStoreServiceStub(self.channel)

    def close(self):
        self.channel.close()

    def __call__(self, session_id, transport_task=None):
        uuid.UUID(session_id)
        session = self.sessions.GetSession(self.pb.GetSessionRequest(session_id=session_id, limit=1),
                                          metadata=self.metadata, timeout=10).session
        # Pinned Substrate transport stores a nil source for direct A2A child
        # sessions. Do not mistake that API omission for a different user. The
        # child ID comes only from the actual root tool response, and we require
        # the exact immutable specialist agent binding as well as user identity.
        if (session.id != session_id or session.user_id != self.metadata[0][1]
                or session.agent_id not in self.agent_ids
                or session.source not in (self.pb.SESSION_SOURCE_AGENT,self.pb.SESSION_SOURCE_UNSPECIFIED)):
            raise ValueError("child session identity mismatch")
        if transport_task is not None:
            # Supplied only from the actual root A2A function_response, after
            # immutable agent and user identity verification above.
            task = transport_task
        else:
            response = self.tasks.ListTasks(self.pb.ListTasksRequest(session_id=session_id),
                                           metadata=self.metadata, timeout=10)
            if len(response.tasks) != 1:
                raise ValueError("ambiguous or missing child task")
            task = self.to_dict(response.tasks[0])
        if not isinstance(task,dict):
            raise ValueError("invalid child task")
        if task.get("contextId") != session_id:
            raise ValueError("child context mismatch")
        return {"result": {"task": task}}
