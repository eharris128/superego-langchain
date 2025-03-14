/**
 * Current Flow Store
 * Manages the currently active flow, its execution state, steps,
 * streaming output, and tool confirmations. Provides a clean API for flow interaction.
 */
import { writable, derived, get } from 'svelte/store';
import { executeFlowStream } from '../api/streamService';
import { getFlowInstance } from '../api/flowService';
import { confirmTool } from '../api/toolService';
import { setLoading, setError } from './uiStateStore';

// Core state stores
const selectedInstanceId = writable<string | null>(null);
const currentSteps = writable<FlowStep[]>([]);

// Tool confirmation state
interface PendingToolConfirmation {
  id: string;
  toolName: string;
  toolInput: any;
  timestamp: number;
}

const pendingToolConfirmations = writable<PendingToolConfirmation[]>([]);

// Streaming and execution state
const executionState = writable<{
  isExecuting: boolean;
  error: string | null;
  flowId: string | null;
  connectionControl: { close: () => void; reconnect: () => void } | null;
}>({
  isExecuting: false,
  error: null,
  flowId: null,
  connectionControl: null
});

// Current partial output for streaming responses
const partialOutput = writable<{
  text: string;
  agentId: string | null;
  complete: boolean;
}>({
  text: '',
  agentId: null,
  complete: true
});

// Derived stores for specific pieces of state
export const isExecuting = derived(executionState, $state => $state.isExecuting);
export const executionError = derived(executionState, $state => $state.error);
export const currentFlowId = derived(executionState, $state => $state.flowId);

// Derived store for the most recent step (for convenience)
export const latestStep = derived(currentSteps, $steps => {
  return $steps.length > 0 ? $steps[$steps.length - 1] : null;
});

// Current streaming output text
export const currentStreamingText = derived(partialOutput, $output => $output.text);

// Indicator if the streaming is complete
export const isStreamingComplete = derived(partialOutput, $output => $output.complete);

/**
 * Select a flow instance and load its data
 * @param instanceId The flow instance ID to select, or null to unselect
 */
async function selectFlowInstance(instanceId: string | null) {
  // Close any existing connection
  const currentConnection = get(executionState).connectionControl;
  if (currentConnection) {
    currentConnection.close();
  }
  
  selectedInstanceId.set(instanceId);
  
  // Reset current state when switching instances
  currentSteps.set([]);
  executionState.set({ 
    isExecuting: false, 
    error: null, 
    flowId: null,
    connectionControl: null 
  });
  partialOutput.set({ text: '', agentId: null, complete: true });
  
  // If we selected a valid instance, load its data
  if (instanceId) {
    try {
      setLoading(true);
      const steps = await getFlowInstance(instanceId);
      currentSteps.set(steps);
      
      // Get the flow ID from the instance ID or metadata if available
      // Since FlowStep doesn't have flow_id, we need to infer it from elsewhere
      const flowId = instanceId.split('-')[0]; // Simple extraction, assuming format like "flowId-instanceHash"
        
      executionState.update(state => ({ ...state, flowId }));
      setLoading(false);
    } catch (error) {
      console.error('Failed to load flow instance:', error);
      setError(`Failed to load flow instance: ${error instanceof Error ? error.message : String(error)}`);
      setLoading(false);
    }
  }
}

/**
 * Execute a message in the current flow
 * @param flowId The flow ID to execute
 * @param message The user message to process
 * @returns Object with cancel function to abort execution
 */
function executeMessage(flowId: string, message: string) {
  // Reset streaming state and set executing
  partialOutput.set({ text: '', agentId: null, complete: false });
  executionState.set({ 
    isExecuting: true, 
    error: null, 
    flowId, 
    connectionControl: null 
  });
  
  // Add the user message as a step
  const userStep: FlowStep = {
    step_id: `user-${Date.now()}`,
    agent_id: 'user',
    timestamp: new Date().toISOString(),
    role: 'user',
    input: null,
    response: message,
    next_agent: null
  };
  
  currentSteps.update(steps => [...steps, userStep]);
  
  // Start the streaming connection
  const connectionControl = executeFlowStream(
    flowId,
    message,
    {
      onPartialOutput: (output) => {
        partialOutput.update(state => ({
          ...state,
          text: output.partial_output,
          complete: !!output.complete
        }));
      },
      
      onCompleteStep: (step) => {
        // Update the agent ID in the partial output
        partialOutput.update(state => ({
          ...state,
          agentId: step.agent_id,
          complete: true
        }));
        
        // Add the complete step to our history
        currentSteps.update(steps => [...steps, step]);
      },
      
      onError: (error) => {
        executionState.update(state => ({ 
          ...state,
          isExecuting: false, 
          error: error.message || 'An error occurred during flow execution'
        }));
        setError(error.message || 'An error occurred during flow execution');
      },
      
      onReconnecting: (attempt, maxAttempts, timeoutMs) => {
        console.log(`Reconnecting... Attempt ${attempt}/${maxAttempts} in ${timeoutMs}ms`);
      },
      
      onMaxReconnectAttemptsReached: () => {
        executionState.update(state => ({
          ...state,
          isExecuting: false,
          error: 'Maximum reconnection attempts reached. Please try again.'
        }));
        setError('Maximum reconnection attempts reached. Please try again.');
      }
    }
  );
  
  // Store the connection control for later use
  executionState.update(state => ({
    ...state,
    connectionControl
  }));
  
  // Return a cancel function
  return {
    cancel: () => {
      connectionControl.close();
      executionState.update(state => ({ 
        ...state, 
        isExecuting: false,
        connectionControl: null
      }));
    }
  };
}

/**
 * Cancel the current execution if one is in progress
 */
function cancelExecution() {
  const state = get(executionState);
  if (state.connectionControl && state.isExecuting) {
    state.connectionControl.close();
    executionState.update(state => ({ 
      ...state, 
      isExecuting: false,
      connectionControl: null
    }));
  }
}

/**
 * Reset the current flow state (used when navigating away or cleanup)
 */
function resetCurrentFlow() {
  // First cancel any active execution
  cancelExecution();
  
  // Then reset all state
  selectedInstanceId.set(null);
  currentSteps.set([]);
  executionState.set({
    isExecuting: false,
    error: null,
    flowId: null,
    connectionControl: null
  });
  partialOutput.set({ text: '', agentId: null, complete: true });
}

export {
  // Core stores
  selectedInstanceId,
  currentSteps,
  executionState,
  partialOutput,
  
  // Functions
  selectFlowInstance,
  executeMessage,
  cancelExecution,
  resetCurrentFlow
};
