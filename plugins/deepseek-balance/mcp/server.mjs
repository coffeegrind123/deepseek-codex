#!/usr/bin/env node

import path from "node:path";
import readline from "node:readline";
import { pathToFileURL } from "node:url";

import {
  DeepSeekBalanceError,
  formatDeepSeekBalance,
  queryDeepSeekBalance,
} from "../lib/deepseek-balance.mjs";

const SERVER_NAME = "deepseek-balance";
const SERVER_VERSION = "0.1.0";
const DEFAULT_PROTOCOL_VERSION = "2024-11-05";
const TOOL_NAME = "get_deepseek_balance";

const TOOL_DEFINITION = {
  name: TOOL_NAME,
  description:
    "Get the current DeepSeek API balance in USD, including total, granted, and topped-up funds and whether API access is available.",
  inputSchema: {
    type: "object",
    properties: {},
    additionalProperties: false,
  },
  annotations: {
    readOnlyHint: true,
    destructiveHint: false,
    idempotentHint: true,
    openWorldHint: true,
  },
};

export function createMcpMessageHandler(options = {}) {
  const env = options.env ?? process.env;
  const fetchImpl = options.fetchImpl ?? globalThis.fetch;

  return async function handleMessage(message) {
    if (
      message === null ||
      typeof message !== "object" ||
      Array.isArray(message) ||
      message.jsonrpc !== "2.0"
    ) {
      return rpcError(null, -32600, "Invalid Request");
    }

    const hasId = Object.prototype.hasOwnProperty.call(message, "id");

    if (!hasId) {
      return null;
    }

    try {
      switch (message.method) {
        case "initialize":
          return rpcResult(message.id, {
            protocolVersion:
              typeof message.params?.protocolVersion === "string"
                ? message.params.protocolVersion
                : DEFAULT_PROTOCOL_VERSION,
            capabilities: {
              tools: {
                listChanged: false,
              },
            },
            serverInfo: {
              name: SERVER_NAME,
              version: SERVER_VERSION,
            },
          });

        case "ping":
          return rpcResult(message.id, {});

        case "tools/list":
          return rpcResult(message.id, {
            tools: [TOOL_DEFINITION],
          });

        case "tools/call":
          return await callTool(
            message.id,
            message.params,
            env,
            fetchImpl,
          );

        default:
          return rpcError(
            message.id,
            -32601,
            `Method not found: ${String(message.method)}`,
          );
      }
    } catch {
      return rpcError(message.id, -32603, "Internal error");
    }
  };
}

async function callTool(id, params, env, fetchImpl) {
  if (params?.name !== TOOL_NAME) {
    return rpcError(id, -32602, "Unknown tool");
  }

  if (
    params.arguments !== undefined &&
    (params.arguments === null ||
      typeof params.arguments !== "object" ||
      Array.isArray(params.arguments) ||
      Object.keys(params.arguments).length !== 0)
  ) {
    return toolError(id, "INVALID_ARGUMENTS", "This tool takes no arguments.");
  }

  try {
    const balance = await queryDeepSeekBalance({
      apiKey: env.DEEPSEEK_API_KEY,
      fetchImpl,
    });

    return rpcResult(id, {
      content: [
        {
          type: "text",
          text: formatDeepSeekBalance(balance),
        },
      ],
      structuredContent: balance,
      isError: false,
    });
  } catch (error) {
    if (error instanceof DeepSeekBalanceError) {
      return toolError(id, error.code, error.message);
    }

    return toolError(
      id,
      "INTERNAL_ERROR",
      "DeepSeek balance query failed unexpectedly.",
    );
  }
}

function toolError(id, code, message) {
  return rpcResult(id, {
    content: [
      {
        type: "text",
        text: `${code}: ${message}`,
      },
    ],
    isError: true,
  });
}

function rpcResult(id, result) {
  return {
    jsonrpc: "2.0",
    id,
    result,
  };
}

function rpcError(id, code, message) {
  return {
    jsonrpc: "2.0",
    id,
    error: {
      code,
      message,
    },
  };
}

export async function runStdioServer(options = {}) {
  const input = options.input ?? process.stdin;
  const output = options.output ?? process.stdout;
  const handleMessage = createMcpMessageHandler(options);
  const lines = readline.createInterface({
    input,
    crlfDelay: Infinity,
    terminal: false,
  });

  for await (const line of lines) {
    if (line.trim() === "") {
      continue;
    }

    let message;
    try {
      message = JSON.parse(line);
    } catch {
      writeMessage(output, rpcError(null, -32700, "Parse error"));
      continue;
    }

    const response = await handleMessage(message);
    if (response !== null) {
      writeMessage(output, response);
    }
  }
}

function writeMessage(output, message) {
  output.write(`${JSON.stringify(message)}\n`);
}

function isMainModule() {
  if (!process.argv[1]) {
    return false;
  }

  return import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href;
}

if (isMainModule()) {
  await runStdioServer();
}
