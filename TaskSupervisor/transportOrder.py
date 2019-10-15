__author__ = "Peter Detzner"
__version__ = "0.0.1a"
__status__ = "Developement"

# IMPORT SYSTEM LIBS
import threading
import time
from random import randint
import uuid
import Queue
import logging
import datetime
import json
import sys
import ast


# IMPORT LOCAL libs
from globals import sanDictQueue, rosMessageDispatcher
import globals


from FiwareObjectConverter.objectFiwareConverter import ObjectFiwareConverter
from Entities.entity import FiwareEntity
from Entities.san import SensorAgent, SensorData

from ROS.moveOrder import rMoveOrder
from ROS.OrderState import OrderState, rosOrderStatus

from TaskSupervisor.transportOrderStateMachine import TransportOrderStateMachine
from TaskSupervisor.transportOrderUpdate import TransportOrderUpdate
from TaskSupervisor.taskInfo import TaskInfo
from TaskSupervisor.taskState import State, TaskState
from TaskSupervisor.triggerValidator import validateTrigger

ocbHandler = globals.ocbHandler

logger = logging.getLogger(__name__)


# defines
ROS_CALL_ERROR = -1
ROS_CALL_SUCCESS = 0
QUEUE_WAIT_TIME_IN_SECONDS = 5


def obj2JsonArray(_obj):
    tempArray = []
    tempArray.append(_obj)
    print json.dumps(tempArray)
    return (tempArray)

class TransportOrder():
    def __init__(self, _taskInfo, _refMaterialflowUpdateId, _refOwnerId):
        # threading.Thread.__init__(self)
        logger.info("Task init")

        # only these attribues will be published
        self.id = str(uuid.uuid4())
        self.refMaterialflowUpdateId = _refMaterialflowUpdateId
        self.refOwnerId = _refOwnerId
        self.taskName = str(_taskInfo.name)
        self.state = State.Idle
        self.startTime = str(datetime.datetime.now())

        

        # setting up the thread
        self._threadRunner = threading.Thread(target=self.run)
        self._threadRunner.setDaemon(True)
        # self._t.start()

        logger.info("Task name: " + self.taskName + ", uuid:" + str(self.id))
        self._taskInfo = _taskInfo 

        # store the names:
        self.fromId =  self._taskInfo.findLocationByTransportOrderStep(self._taskInfo.transportOrders[0].pickupFrom)
        self.toId = self._taskInfo.findLocationByTransportOrderStep(self._taskInfo.transportOrders[0].deliverTo)
        
        # temporary uids
        self._fromUuid = str(uuid.uuid4())
        self._toUuid = str(uuid.uuid4())

        
        # 
        sanDictQueue.addThread(self.id)

        rosMessageDispatcher.addThread(self._fromUuid)
        rosMessageDispatcher.addThread(self._toUuid)

        self._q = sanDictQueue.getQueue(self.id)
        self._rosQ = rosMessageDispatcher.getQueue(self._fromUuid)
        self._rosQTo = rosMessageDispatcher.getQueue(self._toUuid)
        
        self._subscriptionId = None
        self._transportOrderStateMachine = TransportOrderStateMachine(self.taskName)

        self._transportOrderUpdate = TransportOrderUpdate(self)
        logger.info("Task init_done")

    def subscriptionDescription(self):
        return self.taskName + "_" + self.id

# thread imitation
    def start(self):
        logger.info("Task start")
        #self.publishEntity()
        self._threadRunner.start()
        logger.info("Task start_done")

    def join(self):
        logger.info("Task join")
        self._threadRunner.join()
        #self.deleteEntity()
        logger.info("Task join_done")

# updates orion (publish, update and delete)
    def publishEntity(self):
        global ocbHandler
        logger.info("Task publishEntity " + self.taskName)
      #  ocbHandler.create_entity(self.taskState)

        self.updateTime = str(datetime.datetime.now())
        ocbHandler.create_entity(self)
        logger.info("Task publishEntity_done")


    def updateEntity(self):
        global ocbHandler
        logger.info("Task updateEntity " + self.taskName)
        self.updateTime = str(datetime.datetime.now())
        ocbHandler.update_entity(self)
        logger.info("Task updateEntity")

    def deleteEntity(self):
        global ocbHandler
        logger.info("Task deleteEntity " + self.taskName)
        self.updateTime = str(datetime.datetime.now())
        ocbHandler.delete_entity(self.id)
        logger.info("Task deleteEntity_done")


    def __str__(self):
        return "Task name: " + self.taskName + ",uuid: " + str(self.id)

    def __repr__(self):
        return self.__str__()

    def deleteSubscription(self):
        try:
            if(self._subscriptionId):
                ocbHandler.deleteSubscriptionById(self._subscriptionId)
                del globals.subscriptionDict[self._subscriptionId]  
        except Exception as ex:
            pass

    def run(self):
        self.state = State.Running
        #self.updateEntity()
        self._transportOrderUpdate.publishEntity()

        bResendOrder = 1
        
        while(self._transportOrderStateMachine.state != "finished" and self._transportOrderStateMachine.state != "error"):
            
            state = self._transportOrderStateMachine.state
            #print state
            if(state == "init"):
                print state + ": Task " + self.taskName + ", id: " + self.id
                # subscribe to trigger events
                if(self._taskInfo.triggeredBy):
                    ts = SensorAgent()
                    self._subscriptionId = ocbHandler.subscribe2Entity(_description=self.subscriptionDescription(), _entities=obj2JsonArray(ts.getEntity()), _notification=globals.parsedConfigFile.getTaskPlannerAddress() + "/san/" + self.id, _generic=True)
                    globals.subscriptionDict[self._subscriptionId] = self.id
                self._transportOrderStateMachine.Initialized()
            elif(state == "waitForTrigger"):
                print state + ": Task " + self.taskName + ", id: " + self.id 
                if(self._taskInfo.triggeredBy):
                    sensorEntityData = self._q.get()      
                    taskTrigger =  self._taskInfo.triggeredBy[0]
                    retVal = checkIfSensorEventTriggersNextTransportUpdate(sensorEntityData, self._subscriptionId, taskTrigger, self._taskInfo)
                    if(retVal == True):
                        self.deleteSubscription()
                        self._transportOrderStateMachine.TriggerReceived()
                else:
                    # no trigger :-) 
                    self._transportOrderStateMachine.TriggerReceived()                   
                    print state + "_received: Task " + self.taskName + ", id: " + self.id 
            elif(state == "sendAgvToPickup"):
                print state + "_received: Task " + self.taskName + ", id: " + self.id
                if(self.fromId):
                    try:
                        if(bResendOrder == 1): #check if we have to resend it, 
                            rMo = rMoveOrder(self._fromUuid, self.fromId)
                            
                            if(rMo.status == ROS_CALL_SUCCESS): # no need to resend it...
                                bResendOrder = 0
                        rosPacketOrderState = self._rosQ.get(QUEUE_WAIT_TIME_IN_SECONDS)                                
                        if(rosPacketOrderState):
                            tempUuid = rosPacketOrderState.uuid              
                            if((rosPacketOrderState.status == rosOrderStatus.STARTED or rosPacketOrderState.status==rosOrderStatus.ONGOING) and tempUuid==self._fromUuid and bResendOrder == 0):
                                print "Robot is moving" + str(rosPacketOrderState.status)
                                print state + "_Robot_Is_Moving()"+ str(rosPacketOrderState.status)+  "): Task " + self.taskName + ", id: " + self.id
                                self._transportOrderStateMachine.StartMovingToLoadingDestination()
 
                    except Exception:
                        pass 
            elif(state == "moveOrderToPickup"):
                print state + "_received: Task " + self.taskName + ", id: " + self.id 
                try: 
                        ##self.sendMoveOrder(destinationName)
                    rosPacketOrderState = self._rosQ.get()
                    tempUuid = rosPacketOrderState.uuid
                    if(rosPacketOrderState.status == rosOrderStatus.FINISHED and tempUuid ==self._fromUuid):
                        
                        bResendOrder = 1    
                        tmpToPickUp =  self._taskInfo.transportOrderStepInfos[self._taskInfo.transportOrders[0].pickupFrom]
                        if(tmpToPickUp.finishedBy):
                            ts = SensorAgent()
                            self._subscriptionId = ocbHandler.subscribe2Entity(_description=self.subscriptionDescription(), _entities=obj2JsonArray(ts.getEntity()), _notification=globals.parsedConfigFile.getTaskPlannerAddress() + "/san/" + self.id, _generic=True)
                            globals.subscriptionDict[self._subscriptionId] = self.id


                        self._transportOrderStateMachine.AgvArrivedAtLoadingDestination()

                    elif(rosPacketOrderState.status == rosOrderStatus.ERROR or rosPacketOrderState.status == rosOrderStatus.WAITING or rosPacketOrderState.status == rosOrderStatus.UNKNOWN):
                        print rosPacketOrderState.status
                except Queue.Empty:
                    pass
            elif(state == "waitForLoading"):
                print state + "_received: Task " + self.taskName + ", id: " + self.id
                print "NOW LOADDDDDDDDDDDDDDDDDDDDDDDDDDDDd"
                sensorEntityData = self._q.get()
                tmpToPickUp =  self._taskInfo.transportOrderStepInfos[self._taskInfo.transportOrders[0].pickupFrom]
                taskTrigger =  tmpToPickUp.finishedBy[0]
                retVal = checkIfSensorEventTriggersNextTransportUpdate(sensorEntityData, self._subscriptionId, taskTrigger, self._taskInfo)
                if(retVal == True):
                    self.deleteSubscription()
                    self._transportOrderStateMachine.AgvIsLoaded()
 
            elif(state == "sendAgvToDelivery"):
                print state + "_received: Task " + self.taskName + ", id: " + self.id   
                if(self.toId):
                    try:
                        if(bResendOrder == 1): #check if we have to resend it, 
                            rMo = rMoveOrder(self._toUuid, self.toId)
                            
                            if(rMo.status == ROS_CALL_SUCCESS): # no need to resend it...
                                bResendOrder = 0
                        rosPacketOrderState = self._rosQTo.get(QUEUE_WAIT_TIME_IN_SECONDS)                                
                        if(rosPacketOrderState):
                            tempUuid = rosPacketOrderState.uuid              
                            if((rosPacketOrderState.status == rosOrderStatus.STARTED or rosPacketOrderState.status==rosOrderStatus.ONGOING) and tempUuid==self._toUuid and bResendOrder == 0):
                                print "Robot is moving" + str(rosPacketOrderState.status)
                                print state + "_Robot_Is_Moving()"+ str(rosPacketOrderState.status)+  "): Task " + self.taskName + ", id: " + self.id
                                self._transportOrderStateMachine.StartMovingToUnloadingDestination()
 
                    except Exception:
                        pass 
            elif(state == "moveOrderToDelivery"):
                print state + "_received: Task " + self.taskName + ", id: " + self.id
                try: 
                        ##self.sendMoveOrder(destinationName)
                    rosPacketOrderState = self._rosQTo.get()
                    tempUuid = rosPacketOrderState.uuid
                    if(rosPacketOrderState.status == rosOrderStatus.FINISHED and tempUuid ==self._toUuid):
                        
                        bResendOrder = 1    
                        tmpToPickUp =  self._taskInfo.transportOrderStepInfos[self._taskInfo.transportOrders[0].deliverTo]
                        if(tmpToPickUp.finishedBy):
                            ts = SensorAgent()
                            self._subscriptionId = ocbHandler.subscribe2Entity(_description=self.subscriptionDescription(), _entities=obj2JsonArray(ts.getEntity()), _notification=globals.parsedConfigFile.getTaskPlannerAddress() + "/san/" + self.id, _generic=True)
                            globals.subscriptionDict[self._subscriptionId] = self.id

                        self._transportOrderStateMachine.AgvArrivedAtUnloadingDestination()

                    elif(rosPacketOrderState.status == rosOrderStatus.ERROR or rosPacketOrderState.status == rosOrderStatus.WAITING or rosPacketOrderState.status == rosOrderStatus.UNKNOWN):
                        print rosPacketOrderState.status
                except Queue.Empty:
                    pass 
            elif(state == "waitForUnloading"):
                print state + "_received: Task " + self.taskName + ", id: " + self.id 
                
                sensorEntityData = self._q.get()
                tmpToDelivery =  self._taskInfo.transportOrderStepInfos[self._taskInfo.transportOrders[0].deliverTo]
                taskTrigger =  tmpToDelivery.finishedBy[0]
                retVal = checkIfSensorEventTriggersNextTransportUpdate(sensorEntityData, self._subscriptionId, taskTrigger, self._taskInfo)
                if(retVal == True):
                    self.deleteSubscription()
                    self._transportOrderStateMachine.TriggerReceived()
 

            # elif(state == "moveOrderStart"):
            #     print state + ": Task " + self.taskName + ", id: " + self.id 


            #     ## NOT NEEDED ANY MORE
                
            #     if(self.fromId):
            #         try:
            #             if(bResendOrder == 1): 
            #                 rMo = rMoveOrder(self._fromUuid, self.fromId)
            #                 if(rMo.status == ROS_CALL_SUCCESS): # no need to resend it...
            #                     bResendOrder = 2 # send also the "deliverTo" destination
            #                 else:
            #                     bResendOrder = 1 

            #             if(bResendOrder == 2): 
            #                 rMo = rMoveOrder(self._toUuid, self.toId)
            #                 if(rMo.status == ROS_CALL_SUCCESS): # no need to resend it...
            #                     bResendOrder = 0   
            #                 else:
            #                     bResendOrder = 2 # resend deliverTo destination
                                
            #             rosPacketOrderState = self._rosQ.get(QUEUE_WAIT_TIME_IN_SECONDS)
            #             if(rosPacketOrderState):
            #                 tempUuid = rosPacketOrderState.uuid              
            #                 if((rosPacketOrderState.status == rosOrderStatus.STARTED or rosPacketOrderState.status==rosOrderStatus.ONGOING) and tempUuid==self._fromUuid and bResendOrder == 0):
            #                     print "Robot is moving" + str(rosPacketOrderState.status)
            #                     print state + "_Robot_Is_Moving()"+ str(rosPacketOrderState.status)+  "): Task " + self.taskName + ", id: " + self.id
            #                     self._transportOrderStateMachine.OrderStart()                    
 
            #         except Exception:
            #             pass
            # elif(state == "moveOrder"): 
            #     #print state
            #     try: 
            #             ##self.sendMoveOrder(destinationName)
            #         rosPacketOrderState = self._rosQTo.get()
            #         tempUuid = rosPacketOrderState.uuid
            #         if(rosPacketOrderState.status == rosOrderStatus.FINISHED and tempUuid ==self._toUuid):
            #             self._transportOrderStateMachine.OrderFinished()
            #             bResendOrder = True
                        
            #         elif(rosPacketOrderState.status == rosOrderStatus.ERROR or rosPacketOrderState.status == rosOrderStatus.WAITING or rosPacketOrderState.status == rosOrderStatus.UNKNOWN):
            #             print rosPacketOrderState.status
            #     except Queue.Empty:
            #         pass
            elif(state == "moveOrderFinished"):
                print state + "_finished: Task " + self.taskName + ", id: " + self.id
                self._transportOrderStateMachine.DestinationReached()

            #print state 

        logger.info("Task running, " + str(self))

        rosMessageDispatcher.removeThread(self.id)
        sanDictQueue.removeThread(self.id)

        try:
            if(self._subscriptionId):
                ocbHandler.deleteSubscriptionById(self._subscriptionId)
        except Exception as ex:
            pass
        
        self.state = State.Finished
        #self.updateEntity()
        self._transportOrderUpdate.deleteEntity()
        logger.info("Task finished, " + str(self))


def checkIfSensorEventTriggersNextTransportUpdate(sensorEntityData,subscriptionId, taskTrigger, taskInfo):
    if (sensorEntityData):                        
        if("subscriptionId" in sensorEntityData): 
            
            if(sensorEntityData["subscriptionId"] == subscriptionId):
                for sensorData in sensorEntityData["data"]:
                    dd = SensorAgent.CreateObjectFromJson(sensorData)
                    if(taskTrigger): 
                        if(dd.readings): 
                            excpectedType = taskInfo.matchEventNameBySensorId(dd.sensorID, taskTrigger["left"])
                            if excpectedType:
                                if(validateTrigger(excpectedType, dd.readings, taskTrigger)):
                                    return True
    return False
# TASK
# 0= No-Task, 1 = Start, 2 = Pause, 3 = Cancel, 4 = EmergencyStop, 5 = Reset"
# TASK_STATE
# Idle : 0, Running : 1, Waiting : 2, Active : 3, Finished : 4, Aborted : 5, Error : 6


