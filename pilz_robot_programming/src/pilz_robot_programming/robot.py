# Copyright (c) 2018 Pilz GmbH & Co. KG
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""API for easy usage of Pilz robot commands."""

from __future__ import absolute_import

import rospy
from pilz_msgs.msg import MoveGroupSequenceAction
from actionlib import SimpleActionClient, GoalStatus
from moveit_commander import RobotCommander, MoveItCommanderException
from moveit_msgs.msg import MoveItErrorCodes, MoveGroupAction
import time
import threading
from std_srvs.srv import Trigger
import tf

from .move_control_request import _MoveControlState, MoveControlAction,_MoveControlStateMachine
from .commands import _AbstractCmd, _DEFAULT_PLANNING_GROUP, _DEFAULT_TARGET_LINK
from .exceptions import *
from geometry_msgs.msg import Quaternion, PoseStamped, Pose
from std_msgs.msg import Header

__version__ = '1.0.0'


class Robot(object):
    """
    Main component of the API which allows the user to execute robot motion commands and pause, resume or stop the
    execution. The following commands are currently supported:

    * :py:class:`.Ptp`
    * :py:class:`.Lin`
    * :py:class:`.Circ`
    * :py:class:`.Sequence`
    * :py:class:`.Gripper`

    For a more detailed description of the individual commands please see the documentation of
    the corresponding command. Especially see the documentation of the `pilz_trajectory_generation` package
    to get more information on additional parameters that can be configured in the MoveIt! plugin.

    The commands are executed with the help of Moveit.

    :note:
        To any given time only one instance of the Robot class is allowed to exist.

    :note:
        Before you create an instance of :py:class:`.Robot`, ensure that MoveIt is up and running,
        because the constructor blocks until all necessary connections to Moveit are established.
        Currently connections to the following topics have to be established before the function
        finishes:

        * move_group
        * sequence_move_group

    :note:
        Currently the API does not support creating a new instance of :py:class:`.Robot` after deleting an old one in the
        same program. However this can be realized by calling :py:meth:`_release` before the deletion.

    :param version:
        To ensure that always the correct API version is used, it is necessary to state
        which version of the API is expected. If the given version does not match the
        version of the underlying API then an exception is thrown. Only the major version number
        is considered.

    :raises RobotVersionError: if the given version string does not match the module version.
    :raises RobotMultiInstancesError: if an instance of Robot class already exists.
    """

    # ++++++++++++++++++++++++++
    # + Return value constants +
    # ++++++++++++++++++++++++++

    # Command finished successfully
    _SUCCESS = 1
    # Command was stopped; Value based on Moveit error code for preempted
    _STOPPED = -7
    # Something went wrong while executing the command
    _FAILURE = 99999

    # Topic names
    _PAUSE_TOPIC_NAME = "pause_movement"
    _RESUME_TOPIC_NAME = "resume_movement"
    _STOP_TOPIC_NAME = "stop_movement"
    _SEQUENCE_TOPIC = "sequence_move_group"
    _SINGLE_INSTANCE_FLAG = "/robot_api_single_instance_flag"

    def __init__(self, version=None):
        rospy.logdebug("Initialize Robot Api.")

        # tf listener is necessary for pose transformation
        # when using custom reference frames.
        self.tf_listener_ = tf.TransformListener()

        self._move_lock = threading.Lock()

        # manage the move control request
        self._move_ctrl_sm = _MoveControlStateMachine()

        self._ctor_exception_flag = False

        self._check_version(version)

        self._check_single_instance()

        self._establish_connections()

        # We use this auxiliary member to implement a lazy initialization
        # for the '_robot_commander' member. The lazy initialization
        # is necessary to ensure testability.
        self.__robot_commander = None

        # cleanup when ros terminates
        rospy.on_shutdown(self._on_shutdown)

    # The moveit RobotCommander is needed to retrieve robot semantic information.
    # To allow proper testing the RobotCommander is instantiated via lazy initialization.
    @property
    def _robot_commander(self):
        # lazy initialization
        if self.__robot_commander is None:
            self.__robot_commander = RobotCommander()
            rospy.loginfo("RobotCommander created.")
        return self.__robot_commander

    @_robot_commander.setter
    def _robot_commander(self, robot_commander):
        self.__robot_commander = robot_commander

    def get_current_joint_states(self, planning_group=_DEFAULT_PLANNING_GROUP):
        """Returns the current joint state values of the robot.
        :param planning_group: Name of the planning group, default value is "manipulator".
        :return: Returns the current joint values as array
        :rtype: array of floats
        :raises RobotCurrentStateError if given planning group does not exist.
        """
        try:
            return self._robot_commander.get_group(planning_group).get_current_joint_values()
        except MoveItCommanderException as e:
            rospy.logerr(e.message)
            raise RobotCurrentStateError(e.message)

    def get_current_pose(self, target_link=_DEFAULT_TARGET_LINK, base="prbt_base"):
        """Returns the current pose of target link in the reference frame.
        :param target_link: Name of the target_link, default value is "prbt_tcp".
        :param base: The target reference system of the pose, default ist "prbt_base".
        :return: Returns the pose of the given frame
        :rtype: geometry_msgs.msg.Pose
        :raises RobotCurrentStateError if the pose of the given frame is not known
        """

        try:
            self.tf_listener_.waitForTransform(target_link, base, rospy.Time(), rospy.Duration(5, 0))
            orientation_ = Quaternion(0, 0, 0, 1)
            stamped = PoseStamped(header=Header(frame_id=target_link), pose=Pose(orientation=orientation_))
            current_pose = self.tf_listener_.transformPose(base, stamped).pose
            return current_pose
        except tf.Exception as e:
            rospy.logerr(e.message)
            raise RobotCurrentStateError(e.message)

    def move(self, cmd):
        """ Allows the user to start/execute robot motion commands.

         The function blocks until the specified command is completely executed.

         The commands are executed with the help of Moveit.

        :note:
            While :py:meth:`move` is running no further calls to :py:meth:`move` are allowed.

        :param cmd: The robot motion command which has to be executed. The following commands are currently supported:

            * :py:class:`.Ptp`
            * :py:class:`.Lin`
            * :py:class:`.Circ`
            * :py:class:`.Sequence`
            * :py:class:`.Gripper`

        :raises RobotUnknownCommandType: if an unsupported command is passed to the function.
        :raises RobotMoveAlreadyRunningError: if a move command is already running.
        :raises RobotMoveFailed: if the execution of a move command fails.
            Due to the exception any thread or script calling the :py:meth:`move` function
            ends immediately. An exception is thrown instead of returning an error
            to ensure that no further robot motion commands are executed.
        """
        # Check command type
        if not isinstance(cmd, _AbstractCmd):
            rospy.logerr("Unknown command type.")
            raise RobotUnknownCommandType("Unknown command type.")

        # Check that move is not called by multiple threads in parallel.
        if not self._move_lock.acquire(False):
            raise RobotMoveAlreadyRunningError("Parallel calls to move are note allowed.")

        rospy.loginfo("Move: " + cmd.__class__.__name__)
        rospy.logdebug("Move: " + str(cmd))

        # automatic transition from STOP_REQUESTED to NO_REQUEST when move is called
        if self._move_ctrl_sm.state == _MoveControlState.STOP_REQUESTED:
            self._move_ctrl_sm.switch(MoveControlAction.MOTION_STOPPED)

        # automatic transition from RESUME_REQUESTED to NO_REQUEST when move is called
        if self._move_ctrl_sm.state == _MoveControlState.RESUME_REQUESTED:
            self._move_ctrl_sm.switch(MoveControlAction.MOTION_RESUMED)

        try:
            self._move_execution_loop(cmd)
        finally:
            self._move_lock.release()

    def stop(self):
        """The stop function allows the user to cancel the currently running robot motion command and . This is also
        true for a paused command.

        :note:
            Function calls to :py:meth:`move` and :py:meth:`stop` have to be performed from different threads because
            :py:meth:`move` blocks the calling thread.
            The move-thread is terminated. If no motion command is active, the stop-thread is terminated.
        """
        rospy.loginfo("Stop called.")
        self._move_ctrl_sm.switch(MoveControlAction.STOP)
        self._cancel_on_all_clients()

    def pause(self):
        """The pause function allows the user to stop the currently running robot motion command. The :py:meth:`move`
        function then waits for resume. The motion can still be canceled using :py:meth:`stop`.

        :note:
            Function calls to :py:meth:`move` and :py:meth:`pause` have to be performed from different threads because
            :py:meth:`move` blocks the calling thread.
        """
        rospy.loginfo("Pause called.")
        self._move_ctrl_sm.switch(MoveControlAction.PAUSE)

        # Ensure that all running commands are cancelled/stopped
        self._cancel_on_all_clients()

    def resume(self):
        """The function resumes a paused robot motion. If the motion command is not paused or no motion command is active,
        it has no effects.

        :note:
            Function calls to :py:meth:`move` and :py:meth:`resume` have to be performed from different threads because
            :py:meth:`move` blocks the calling thread.
        """
        rospy.loginfo("Resume called.")
        self._move_ctrl_sm.switch(MoveControlAction.RESUME)

    def _move_execution_loop(self, cmd):

        continue_execution_of_cmd = True
        first_iteration_flag = True

        while continue_execution_of_cmd:
            rospy.logdebug("Move execution loop.")

            # execute
            if ((self._move_ctrl_sm.state == _MoveControlState.NO_REQUEST and first_iteration_flag)
                or self._move_ctrl_sm.state == _MoveControlState.RESUME_REQUESTED) \
                    and continue_execution_of_cmd:
                rospy.logdebug("start execute")

                # automatic switch to no request
                if self._move_ctrl_sm.state == _MoveControlState.RESUME_REQUESTED:
                    self._move_ctrl_sm.switch(MoveControlAction.MOTION_RESUMED)

                execution_result = cmd._execute(self)

                # evaluate the result of execute
                # motion preempt
                if execution_result == Robot._STOPPED:
                    # need to wait for resume, or execute the motion again
                    if self._move_ctrl_sm.state == _MoveControlState.PAUSE_REQUESTED \
                            or self._move_ctrl_sm.state == _MoveControlState.RESUME_REQUESTED:
                        continue
                    # external stop
                    elif self._move_ctrl_sm.state == _MoveControlState.NO_REQUEST:
                        rospy.logerr("External stop of move command")
                        raise RobotMoveFailed("External stop of move command")
                    # normal stop
                    else:
                        rospy.logerr("Execution of move command is stopped")
                        raise RobotMoveFailed("Execution of move command is stopped")
                # motion succeeded
                elif execution_result == Robot._SUCCESS:
                    continue_execution_of_cmd = False
                # motion failed
                else:
                    rospy.logerr("Failure during execution of: " + str(cmd))
                    raise RobotMoveFailed("Failure during execution of: " + str(cmd))

            # pause
            if self._move_ctrl_sm.state == _MoveControlState.PAUSE_REQUESTED:
                rospy.loginfo("start wait for resume")
                self._move_ctrl_sm.wait_for_resume()

            # stop
            if self._move_ctrl_sm.state == _MoveControlState.STOP_REQUESTED:
                rospy.logerr("Execution of move command is stopped")
                raise RobotMoveFailed("Execution of move command is stopped")

            first_iteration_flag = False

    def _on_shutdown(self):
        def delete_param(key):
            if rospy.has_param(key):
                rospy.logdebug("Delete parameter " + key + " from parameter server.")
                rospy.delete_param(key)
        # deletes the single instance parameter when interpreter terminates
        delete_param(self._SINGLE_INSTANCE_FLAG)

        with self._move_ctrl_sm: # wait, if _execute is just starting a send_goal()
            actionclient_state = self._sequence_client.get_state()
        # stop movement
        if actionclient_state != GoalStatus.LOST: # is the client currently tracking a goal?
            self._sequence_client.cancel_goal()
            self._sequence_client.wait_for_result(timeout = rospy.Duration(2.))

    def _cancel_on_all_clients(self):
        self._sequence_client.cancel_goal()

    def _pause_service_callback(self, request):
        self.pause()
        return [True, "success"]

    def _resume_service_callback(self, request):
        self.resume()
        return [True, "success"]

    def _stop_service_callback(self, request):
        self.stop()
        return [True, "success"]

    def _map_error_code(self, moveit_error_code):
        """Maps the given Moveit error code to API specific return values."""
        if moveit_error_code.val == MoveItErrorCodes.SUCCESS:
            return self._SUCCESS
        elif moveit_error_code.val == MoveItErrorCodes.PREEMPTED:
            return self._STOPPED
        else:
            return self._FAILURE

    def _check_version(self, version):
        # check if version is set by user
        if version is None:
            rospy.logerr("Version of Robot API is not set!")
            self._ctor_exception_flag = True
            raise RobotVersionError("Version of Robot API is not set!"
                                    "Current installed version is " + __version__ + "!")

        # check given version is correct
        if version != __version__.split(".")[0]:
            rospy.logerr("Version of Robot API does not match!")
            self._ctor_exception_flag = True
            raise RobotVersionError("Version of Robot API does not match! "
                                    "Current installed version is " + __version__ + "!")

    def _check_single_instance(self):
        # If running the same program twice the second should kill the first, however the parameter server
        # has a small delay so we check twice for the single instance flag.
        if rospy.has_param(self._SINGLE_INSTANCE_FLAG):
            time.sleep(1)

        if rospy.has_param(self._SINGLE_INSTANCE_FLAG) and rospy.get_param(self._SINGLE_INSTANCE_FLAG):
            rospy.logerr("An instance of Robot class already exists.")
            self._ctor_exception_flag = True
            raise RobotMultiInstancesError("Only one instance of Robot class can be created!")
        else:
            rospy.set_param(self._SINGLE_INSTANCE_FLAG, True)

    def _establish_connections(self):
        # Create sequence_move_group client, only for manipulator
        self._sequence_client = SimpleActionClient(self._SEQUENCE_TOPIC, MoveGroupSequenceAction)
        rospy.loginfo("Waiting for connection to action server " + self._SEQUENCE_TOPIC + "...")
        self._sequence_client.wait_for_server()
        rospy.logdebug("Connection to action server " + self._SEQUENCE_TOPIC + " established.")

        # Start ROS Services which allow to pause, resume and stop movements
        self._pause_service = rospy.Service(Robot._PAUSE_TOPIC_NAME, Trigger, self._pause_service_callback)
        self._resume_service = rospy.Service(Robot._RESUME_TOPIC_NAME, Trigger, self._resume_service_callback)
        self._stop_service = rospy.Service(Robot._STOP_TOPIC_NAME, Trigger, self._stop_service_callback)

    def _release(self):
        rospy.logdebug("Release called")
        try:
            self._pause_service.shutdown(reason="Robot instance released.")
            self._resume_service.shutdown(reason="Robot instance released.")
            self._stop_service.shutdown(reason="Robot instance released.")
        except AttributeError:
            rospy.logdebug("Services do not exists yet or have already been shutdown.")
        # if robot is not created successfully, do not change the flag
        if not self._ctor_exception_flag:
            rospy.logdebug("Delete single instance parameter from parameter server.")
            rospy.delete_param(self._SINGLE_INSTANCE_FLAG)

    def __del__(self):
        rospy.logdebug("Dtor called")
        self._release()
