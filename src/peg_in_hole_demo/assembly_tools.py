#!/usr/bin/env python

# Imports for ros
from inspect import EndOfBlock
from operator import truediv
import rospy
# import tf
import numpy as np
import matplotlib.pyplot as plt
from rospkg import RosPack
from geometry_msgs.msg import WrenchStamped, Wrench, TransformStamped, PoseStamped, Pose, Point, Quaternion, Vector3, Transform
from rospy.core import configure_logging

from sensor_msgs.msg import JointState
# from assembly_ros.srv import ExecuteStart, ExecuteRestart, ExecuteStop
from controller_manager_msgs.srv import SwitchController, LoadController, ListControllers

import tf2_ros
# import tf2
import tf2_geometry_msgs
import tf.transformations as trfm

from threading import Lock


#State names    
IDLE_STATE           = 'idle'
CHECK_FEEDBACK_STATE = 'checking load cell feedback'
APPROACH_STATE       = 'approaching hole surface'
FIND_HOLE_STATE      = 'finding hole'
INSERTING_PEG_STATE  = 'inserting peg'
COMPLETION_STATE     = 'completed insertion'
SAFETY_RETRACT_STATE = 'retracting to safety' 

#Trigger names
CHECK_FEEDBACK_TRIGGER     = 'check loadcell feedback'
APPROACH_SURFACE_TRIGGER   = 'start approach'
FIND_HOLE_TRIGGER          = 'surface found'
INSERT_PEG_TRIGGER         = 'hole found'
ASSEMBLY_COMPLETED_TRIGGER = 'assembly completed'
SAFETY_RETRACTION_TRIGGER  = 'retract to safety'


class AssemblyTools():

    def __init__(self, ROS_rate, start_time):

        self._wrench_pub    = rospy.Publisher('/cartesian_compliance_controller/target_wrench', WrenchStamped, queue_size=10)
        self._pose_pub      = rospy.Publisher('cartesian_compliance_controller/target_frame', PoseStamped , queue_size=2)
        self._target_pub    = rospy.Publisher('target_hole_position', PoseStamped, queue_size=2, latch=True)
        self._ft_sensor_sub = rospy.Subscriber("/cartesian_compliance_controller/ft_sensor_wrench/", WrenchStamped, self.callback_update_wrench, queue_size=2)
        # self._tcp_pub   = rospy.Publisher('target_hole_position', PoseStamped, queue_size=2, latch=True)

        #Needed to get current pose of the robot
        self.tf_buffer = tf2_ros.Buffer(rospy.Duration(1200.0)) #tf buffer length
        self.tf_listener = tf2_ros.TransformListener(self.tf_buffer)
        self.broadcaster = tf2_ros.StaticTransformBroadcaster()

        #job parameters moved in from the peg_in_hole_params.yaml file
        #'peg_4mm' 'peg_8mm' 'peg_10mm' 'peg_16mm'
        #'hole_4mm' 'hole_8mm' 'hole_10mm' 'hole_16mm'
        self.target_peg = 'peg_10mm'
        self.target_hole = 'hole_10mm'
        self.activeTCP = "tool0"
        self.activeTCP_Title = self.target_peg


        self._rate_selected = ROS_rate
        self._rate = rospy.Rate(self._rate_selected) #setup for sleeping in hz
        self._seq = 0
        self._start_time = start_time #for _spiral_search_basic_force_control and spiral_search_basic_compliance_control
        
        #Spiral parameters
        self._freq = np.double(0.15) #Hz frequency in _spiral_search_basic_force_control
        self._amp  = np.double(10.0)  #Newton amplitude in _spiral_search_basic_force_control
        self._first_wrench = self.create_wrench([0,0,0], [0,0,0])
        self._freq_c = np.double(0.15) #Hz frequency in spiral_search_basic_compliance_control
        self._amp_c  = np.double(.002)  #meters amplitude in spiral_search_basic_compliance_control
        self._amp_limit_c = 2 * np.pi * 10 #search number of radii distance outward
        
        # # Establish goal position -- TODO: Analyse whether redundant  
        # self.readBoardPosition()
        # self._target_pub.publish(self.target_hole_pose)
        # self.x_pos_offset = self.target_hole_pose.pose.position.x
        # self.y_pos_offset = self.target_hole_pose.pose.position.y
        
        #generate helpful transform matrix for later
        self.tool_data = dict()
        self.readYAML()


        #loop parameters
        self.curr_time = rospy.get_rostime() - self._start_time
        self.curr_time_numpy = np.double(self.curr_time.to_sec())
        self.wrench_vec  = self.get_command_wrench([0,0,0])
        self.next_trigger = '' #Empty to start. Each callback should decide what next trigger to implement in the main loop

        self.current_pose = self.get_current_pos()
        self.pose_vec = self.full_compliance_position()
        self.current_wrench = self._first_wrench
        self._average_wrench = self._first_wrench.wrench 
        self._bias_wrench = self._first_wrench.wrench #Calculated to remove the steady-state error from wrench readings. 
        #TODO - subtract bias_wrench from the "current wrench" callback; Tried it but performance was unstable.
        self.average_speed = np.array([0.0,0.0,0.0])
 
        self.highForceWarning = False
        self.surface_height = 0.0
        self.restart_height = .1
        self.collision_confidence = 0;

        #Simple Moving Average Parameters
        self._buffer_window = self._rate_selected #self._rate_selected = 1/Hz since this variable is the rate of ROS commands
        self._data_buffer = []
        # self._moving_avg_data = np. #Empty to start. make larger than we need since np is contiguous memory. Will ignore NaN values.
        # self._data_buffer = np.empty(self._buffer_window)
        # self.avg_it = 0#iterator for allocating the first window in the moving average calculation
        # self._data_buffer = np.zeros(self._buffer_window)
        # self._moving_avg_data = [] #Empty to start

   

    def readYAML(self):
        """Read data from job config YAML and make certain calculations for later use. Stores peg frames in dictionary tool_data
        """
        
        self.read_board_positions()
        
        self.read_peg_hole_dimensions()

        #Calculate transform from TCP to peg corner        
        self.peg_locations   = rospy.get_param('/objects/'+self.target_peg+'/grasping/pinch_grasping/locations')
        
        # Setup default zero-transform in case it needs to be referenced for consistency.
        self.tool_data['tool0'] = dict()
        a = self.tf_buffer.lookup_transform("tool0", "tool0", rospy.Time(0), rospy.Duration(100.0))
        self.tool_data['tool0']['transform']    = a
        self.tool_data['tool0']['matrix']       = AssemblyTools.to_homogeneous(a.transform.rotation, a.transform.translation)

        for key in list(self.peg_locations):
            #Write the position of the peg's corner wrt the gripper tip as a reference-ready TF.
            pegTransform = AssemblyTools.get_tf_from_YAML(self.peg_locations[str(key)]['pose'], self.peg_locations[str(key)]['orientation'],
            "tool0_to_gripper_tip_link", "peg_"+str(key)+"_position")
            self.broadcaster.sendTransform(pegTransform)
            self._rate.sleep()
            a = self.tf_buffer.lookup_transform("tool0", "peg_"+str(key)+"_position", rospy.Time(0), rospy.Duration(100.0))
            # a = self.tf_buffer.lookup_transform("tool0", 'peg_corner_position', rospy.Time(0), rospy.Duration(100.0))
            self.tool_data[str(key)]=dict()
            self.tool_data[str(key)]['transform']   = a
            self.tool_data[str(key)]['matrix']      = AssemblyTools.to_homogeneous(a.transform.rotation, a.transform.translation)
            rospy.logerr("Added TCP entry for " + str(key))
            # rospy.logwarn('Transform for ' + self.target_peg + ' is ' + str(a) + " and that gives a homog matrix of " + str(self.tool_data[self.target_peg + '_matrix']))
            # b = AssemblyTools.matrix_to_tf(self.tool_data[self.target_peg + '_matrix'], 'tool0', 'peg_corner_position')
            # rospy.logwarn('Converting back to Transform! Result: ' + str(b))
        
        rospy.logerr("TCP position dictionary now contains: " + str(list(self.tool_data)))
        # quit()

    def read_board_positions(self):
        """ Calculates pose of target hole relative to robot base frame.
        """
        temp_z_position_offset = 207 #Our robot is reading Z positions wrong on the pendant for some reason.
        taskPos = list(np.array(rospy.get_param('/environment_state/task_frame/position')))
        taskPos[2] = taskPos[2] + temp_z_position_offset
        taskOri = rospy.get_param('/environment_state/task_frame/orientation')
        holePos = list(np.array(rospy.get_param('/objects/'+self.target_hole+'/local_position')))
        holePos[2] = holePos[2] + temp_z_position_offset
        holeOri = rospy.get_param('/objects/'+self.target_hole+'/local_orientation')
        
        #Set up target hole pose
        self.tf_robot_to_task_board = AssemblyTools.get_tf_from_YAML(taskPos, taskOri, "base_link", "task_board")
        self.pose_task_board_to_hole = AssemblyTools.get_pose_from_YAML(holePos, holeOri, "base_link")
        self.target_hole_pose = tf2_geometry_msgs.do_transform_pose(self.pose_task_board_to_hole, self.tf_robot_to_task_board)
        self._target_pub.publish(self.target_hole_pose)
        self.x_pos_offset = self.target_hole_pose.pose.position.x
        self.y_pos_offset = self.target_hole_pose.pose.position.y

        # temp_z_position_offset = 207/1000 #Our robot is reading Z positions wrong on the pendant for some reason.
        # taskPos = list(np.array(rospy.get_param('/environment_state/task_frame/position'))/1000)
        # taskPos[2] = taskPos[2] + temp_z_position_offset
        # taskOri = rospy.get_param('/environment_state/task_frame/orientation')
        # holePos = list(np.array(rospy.get_param('/objects/'+self.target_hole+'/local_position'))/1000)
        # holePos[2] = holePos[2] + temp_z_position_offset
        # holeOri = rospy.get_param('/objects/'+self.target_hole+'/local_orientation')

        # self.tf_robot_to_task_board = TransformStamped() #tf_task_board_to_hole
        # self.tf_robot_to_task_board.header.stamp = rospy.get_rostime()
        # self.tf_robot_to_task_board.header.frame_id = "base_link"
        # self.tf_robot_to_task_board.child_frame_id = "task_board"
        # tempQ = list(trfm.quaternion_from_euler(taskOri[0]*np.pi/180, taskOri[1]*np.pi/180, taskOri[2]*np.pi/180))
        # self.tf_robot_to_task_board.transform = Transform(Point(taskPos[0],taskPos[1],taskPos[2]) , Quaternion(tempQ[0], tempQ[1], tempQ[2], tempQ[3]))
        
        # self.pose_task_board_to_hole = PoseStamped() #tf_task_board_to_hole
        # self.pose_task_board_to_hole.header.stamp = rospy.get_rostime()
        # self.pose_task_board_to_hole.header.frame_id = "task_board"
        # tempQ = list(trfm.quaternion_from_euler(holeOri[0]*np.pi/180, holeOri[1]*np.pi/180, holeOri[2]*np.pi/180))
        # self.pose_task_board_to_hole.pose = Pose(Point(holePos[0],holePos[1],holePos[2]), Quaternion(tempQ[0], tempQ[1], tempQ[2], tempQ[3]))
        
        # self.target_hole_pose = tf2_geometry_msgs.do_transform_pose(self.pose_task_board_to_hole, self.tf_robot_to_task_board)

    def read_peg_hole_dimensions(self):
        """Read peg and hole data from YAML configuration file.
        """
        peg_diameter         = rospy.get_param('/objects/'+self.target_peg+'/dimensions/diameter')/1000 #mm
        peg_tol_plus         = rospy.get_param('/objects/'+self.target_peg+'/tolerance/upper_tolerance')/1000
        peg_tol_minus        = rospy.get_param('/objects/'+self.target_peg+'/tolerance/lower_tolerance')/1000
        hole_diameter        = rospy.get_param('/objects/'+self.target_hole+'/dimensions/diameter')/1000 #mm
        hole_tol_plus        = rospy.get_param('/objects/'+self.target_hole+'/tolerance/upper_tolerance')/1000
        hole_tol_minus       = rospy.get_param('/objects/'+self.target_hole+'/tolerance/lower_tolerance')/1000    
        self.hole_depth      = rospy.get_param('/objects/'+self.target_peg+'/dimensions/min_insertion_depth')/1000
        
        #setup, run to calculate useful values based on params:
        self.clearance_max = hole_tol_plus - peg_tol_minus #calculate the total error zone;
        self.clearance_min = hole_tol_minus + peg_tol_plus #calculate minimum clearance;     =0
        self.clearance_avg = .5 * (self.clearance_max- self.clearance_min) #provisional calculation of "wiggle room"
        self.safe_clearance = (hole_diameter-peg_diameter + self.clearance_min)/2; # = .2 *radial* clearance i.e. on each side.
        # rospy.logerr("Peg is " + str(self.target_peg) + " and hole is " + str(self.target_hole))
        # rospy.logerr("Spiral pitch is gonna be " + str(self.safe_clearance) + "because that's min tolerance " + str(self.clearance_min) + " plus gap of " + str(hole_diameter-peg_diameter))
            
    @staticmethod
    def get_tf_from_YAML(pos, ori, base_frame, child_frame): #Returns the transform from base_frame to child_frame based on vector inputs
        """Reads a TF from config YAML.
        :param pos: (string) Param key for desired position parameter.
        :param ori: (string) Param key for desired orientation parameter.
        :param base_frame: (string) Base frame for output TF.
        :param child_frame:  (string) Child frame for output TF.
        :return: Geometry_Msgs.TransformStamped with linked parameters.
        """
        
        output_pose = AssemblyTools.get_pose_from_YAML(pos, ori, base_frame) #tf_task_board_to_hole
        output_tf = TransformStamped()
        output_tf.header = output_pose.header
        #output_tf.transform.translation = output_pose.pose.position
        [output_tf.transform.translation.x, output_tf.transform.translation.y, output_tf.transform.translation.z] = [output_pose.pose.position.x, output_pose.pose.position.y, output_pose.pose.position.z]
        output_tf.transform.rotation   = output_pose.pose.orientation
        output_tf.child_frame_id = child_frame
        
        return output_tf
    @staticmethod
    def get_pose_from_YAML(pos, ori, base_frame): #Returns the pose wrt base_frame based on vector inputs.
        """Reads a Pose from config YAML.
        :param pos: (string) Param key for desired position parameter.
        :param ori: (string) Param key for desired orientation parameter.
        :param base_frame: (string) Base frame for output pose.
        :param child_frame:  (string) Child frame for output pose.
        :return: Geometry_Msgs.PoseStamped with linked parameters.
        """
        
        #Inputs are in mm XYZ and degrees RPY
        #move to utils
        output_pose = PoseStamped() #tf_task_board_to_hole
        output_pose.header.stamp = rospy.get_rostime()
        output_pose.header.frame_id = base_frame
        tempQ = list(trfm.quaternion_from_euler(ori[0]*np.pi/180, ori[1]*np.pi/180, ori[2]*np.pi/180))
        output_pose.pose = Pose(Point(pos[0]/1000,pos[1]/1000,pos[2]/1000) , Quaternion(tempQ[0], tempQ[1], tempQ[2], tempQ[3]))
        
        return output_pose
    
    def select_tool(self, tool_name):
        """Sets activeTCP frame according to title of desired peg frame (tip, middle, etc.). This frame must be included in the YAML.
        :param tool_name: (string) Key in tool_data dictionary for desired frame.
        """
        if(tool_name in list(self.tool_data)):
            self.activeTCP = tool_name
            self.broadcaster.sendTransform(self.tool_data[self.activeTCP]['transform'])

    def spiral_search_basic_compliance_control(self):
        """Generates position, orientation offset vectors which describe a plane spiral about z; 
        Adds this offset to the current approach vector to create a searching pattern. Constants come from Init;
        x,y vector currently comes from x_ and y_pos_offset variables.
        """
        curr_time = rospy.get_rostime() - self._start_time
        curr_time_numpy = np.double(curr_time.to_sec())
        curr_amp = self._amp_c + self.safe_clearance * np.mod(2.0 * np.pi * self._freq_c *curr_time_numpy, self._amp_limit_c);

        # x_pos_offset = 0.88 #TODO:Assume the part needs to be inserted here at the offset. Fix with real value later
        # y_pos_offset = 0.550 #TODO:Assume the part needs to be inserted here at the offset. Fix with real value later
        
        # self._amp_c = self._amp_c * (curr_time_numpy * 0.001 * curr_time_numpy+ 1)

        x_pos = curr_amp * np.cos(2.0 * np.pi * self._freq_c *curr_time_numpy)
        x_pos = x_pos + self.x_pos_offset

        y_pos = curr_amp * np.sin(2.0 * np.pi * self._freq_c *curr_time_numpy)
        y_pos = y_pos + self.y_pos_offset

        # z_pos = 0.2 #0.104 is the approximate height of the hole itself. TODO:Assume the part needs to be inserted here. Update once I know the real value 
        z_pos = self.current_pose.transform.translation.z #0.104 is the approximate height of the hole itself. TODO:Assume the part needs to be inserted here. Update once I know the real value

        pose_position = [x_pos, y_pos, z_pos]

        pose_orientation = [0, 1, 0, 0] # w, x, y, z

        return [pose_position, pose_orientation]

    def linear_search_position(self, direction_vector = [0,0,0], desired_orientation = [0, 1, 0, 0]):
        """Generates a command pose vector which causes the robot to hold a certain orientation
         and comply in z while maintaining the approach vector along x_ and y_pos_offset.
        :param direction_vector: (list of floats) vector directional offset from normal position. Causes constant motion in z.
        :param desired_orientation: (list of floats) quaternion parameters for orientation. 
        """
        pose_position = self.current_pose.transform.translation
        pose_position.x = self.x_pos_offset + direction_vector[0]
        pose_position.y = self.y_pos_offset + direction_vector[1]
        pose_position.z = pose_position.z + direction_vector[2]
        pose_orientation = desired_orientation
        return [[pose_position.x, pose_position.y, pose_position.z], pose_orientation]

    def full_compliance_position(self, direction_vector = [0,0,0], desired_orientation = [0, 1, 0, 0]):
        """Generates a command pose vector which causes the robot to hold a certain orientation
         and comply translationally in all directions.
        :param direction_vector: (list of floats) vector directional offset from normal position. Causes constant motion.
        :param desired_orientation: (list of floats) quaternion parameters for orientation. 
        """ 
        pose_position = self.current_pose.transform.translation
        pose_position.x = pose_position.x + direction_vector[0]
        pose_position.y = pose_position.y + direction_vector[1]
        pose_position.z = pose_position.z + direction_vector[2]
        pose_orientation = desired_orientation
        return [[pose_position.x, pose_position.y, pose_position.z], pose_orientation]

        #Load cell current data

    def callback_update_wrench(self, data):
        """Callback to update current wrench data whenever new data becomes available.
        """
        self.current_wrench = data
        # rospy.loginfo_once("Callback working! " + str(data))

    def post_action(self, trigger_name):
        """Defines the next trigger which the state machine should execute.
        """
        return [trigger_name, True]

    def subtract_vector3s(self, vec1, vec2):

        newVector3 = Vector3(vec1.x - vec2.x, vec1.y - vec2.y, vec1.z - vec2.z)
        return newVector3

    def get_current_pos(self):
        """Read in current pose from robot base to activeTCP.        
        """
        transform = TransformStamped() #TODO: Check that this worked.
        # if(type(offset) == str):
        #     transform = self.tf_buffer.lookup_transform("base_link", self.activeTCP, rospy.Time(0), rospy.Duration(100.0))
        # else:
        transform = self.tf_buffer.lookup_transform("base_link", self.tool_data[self.activeTCP]['transform'].child_frame_id, rospy.Time(0), rospy.Duration(100.0))
        return transform

    def get_command_wrench(self, vec = [0,0,0], ori = [0,0,0]):
        """Output ROS wrench parameters from human-readable vector inputs. 
        :param vec: (list of floats) Vector of desired force in each direction (in Newtons).
        :param ori: (list of floats) Vector of desired torque about each axis (in N*m)
        """

        return [vec[0], vec[1], vec[2], ori[0], ori[1], ori[2]]

    # TODO: Determine if removing the following has changed program at all.
    # def _calibrate_force_zero(self):
    #     curr_time = rospy.get_rostime() - self._start_time
    #     curr_time_numpy = np.double(curr_time.to_sec())

    def publish_wrench(self, input_vec):
        """Publish the commanded wrench to the command topic.
        """
        # self.check_controller(self.force_controller)
        # forces, torques = self.com_to_tcp(result[:3], result[3:], transform)
        # result_wrench = self.create_wrench(result[:3], result[3:])
        # result_wrench = self.create_wrench([7,0,0], [0,0,0])
        result_wrench = self.create_wrench(input_vec[:3], input_vec[3:])
        
        self._wrench_pub.publish(result_wrench)

    # def publish_pose(self, position, orientation):
    def publish_pose(self, pose_stamped_vec):
        """Takes in vector representations of position 
        :param pose_stamped_vec: (list of floats) List of parameters for pose with x,y,z position and orientation quaternion
        """
        # Ensure controller is loaded
        # self.check_controller(self.controller_name)

        # Create poseStamped msg
        goal_pose = PoseStamped()

        # Set the position and orientation
        point = Point()
        quaternion = Quaternion()

        # point.x, point.y, point.z = position
        point.x, point.y, point.z = pose_stamped_vec[0][:]
        goal_pose.pose.position = point

        quaternion.w, quaternion.x, quaternion.y, quaternion.z  = pose_stamped_vec[1][:]
        goal_pose.pose.orientation = quaternion

        # Set header values
        goal_pose.header.stamp = rospy.get_rostime()
        goal_pose.header.frame_id = "base_link"
        
        if(self.activeTCP != "tool0"):
            #Convert pose in TCP coordinates to assign wrist "tool0" position for controller

            b_link = goal_pose.header.frame_id
            goal_matrix = AssemblyTools.to_homogeneous(goal_pose.pose.orientation, goal_pose.pose.position) #tf from base_link to tcp_goal = bTg
            backing_mx = trfm.inverse_matrix(self.tool_data[self.activeTCP]['matrix']) #tf from tcp_goal to wrist = gTw
            goal_matrix = np.dot(goal_matrix, backing_mx) #bTg * gTw = bTw
            goal_pose = AssemblyTools.matrix_to_pose(goal_matrix, b_link)
            
            # self._tool_offset_pub.publish(goal_pose)

            
        self._pose_pub.publish(goal_pose)

    @staticmethod
    def to_homogeneous(quat, point):
        """Takes a quaternion and msg.Point and outputs a homog. tf matrix.
        :param quat: (geometry_msgs.Quaternion) Orientation information.
        :param point: (geometry.msgs.Point) Position information.
        :return: (np.Array()) 4x4 Homogeneous transform matrix.
        """
        #TODO candidate for Utils 
        output = trfm.quaternion_matrix(np.array([quat.x, quat.y, quat.z, quat.w]))
        output[0][3] = point.x
        output[1][3] = point.y
        output[2][3] = point.z
        return output
    
    @staticmethod
    def matrix_to_pose(input, base_frame):
        """Converts matrix into a pose.
        :param input: (np.Array) 4x4 homogeneous transformation matrix
        :param base_frame: (string) base frame for new pose.
        :return: (geometry_msgs.PoseStamped) Pose based on input.
        """
        output = PoseStamped()
        output.header.stamp = rospy.get_rostime()
        output.header.frame_id = base_frame

        quat = trfm.quaternion_from_matrix(input)
        output.pose.orientation.x = quat[0]
        output.pose.orientation.y = quat[1]
        output.pose.orientation.z = quat[2]
        output.pose.orientation.w = quat[3]
        output.pose.position.x = input[0][3]
        output.pose.position.y = input[1][3]
        output.pose.position.z = input[2][3]
        return output
    
    @staticmethod
    def matrix_to_tf(input, base_frame, child_frame):
        """Converts matrix back into a TF.
        :param input: (np.Array) 4x4 homogeneous transformation matrix
        :param base_frame: (string) base frame for new pose.
        :return: (geometry_msgs.TransformStamped) Transform based on input.
        """
        pose = AssemblyTools.matrix_to_pose(input, base_frame)
        output = AssemblyTools.swap_pose_tf(pose, child_frame)
        return output

    @staticmethod
    def swap_pose_tf(input, child_frame):
        """Swaps pose for tf and vice-versa.
        :param input: (geometry_msgs.PoseStamped or geometry_msgs.TransformStamped) Input data type.
        :param child_frame: (string) Child frame name if converting Pose to Transform.
        :return: (geometry_msgs.TransformStamped or geometry_msgs.PoseStamped) Output data, of the other type from input.
        """
        if('PoseStamped' in str(type(input))):
            output = TransformStamped()
            output.header = input.header
            output.transform = input.pose
            output.child_frame_id = child_frame
            return output
        else:
            if('TransformStamped' in str(type(input))):
                output = PoseStamped()
                output.header = input.header
                output.pose = input.transform
                return output
        rospy.logerr("Invalid input to swap_pose_tf !!!")

    def create_wrench(self, force, torque):
        """Composes a standard wrench object from human-readable vectors.
        :param force: (list of floats) x,y,z force values
        :param torque: (list of floats) torques about x,y,z
        :return: (geometry.msgs.WrenchStamped) Output wrench.
        """
        wrench_stamped = WrenchStamped()
        wrench = Wrench()

        # create wrench
        wrench.force.x, wrench.force.y, wrench.force.z = force
        wrench.torque.x, wrench.torque.y, wrench.torque.z = torque

        # create header
        wrench_stamped.header.seq = self._seq

        wrench_stamped.header.stamp = rospy.get_rostime()
        wrench_stamped.header.frame_id = "base_link"
        self._seq+=1

        wrench_stamped.wrench = wrench

        return wrench_stamped

    def update_average_wrench(self):
        """Create a very simple moving average of the incoming wrench readings and store it as self.average.wrench.
        """
        #self._average_wrench = self.weighted_average_wrenches(self._average_wrench, 9, self.current_wrench.wrench, 1)
        self._average_wrench = self.weighted_average_wrenches(self._average_wrench, 9, self.current_wrench.wrench, 1)
        #rospy.logwarn_throttle(.5, "Updating wrench toward " + str(self.current_wrench.wrench.force))

    def weighted_average_wrenches(self, wrench1, scale1, wrench2, scale2):
        """Returns a simple linear interpolation between wrenches.
        :param wrench1:(geometry_msgs.WrenchStamped) First input wrench
        :param scale1: (float) Weight of first input wrench
        :param wrench2:(geometry_msgs.WrenchStamped) Second input wrench
        :param scale2: (float) Weight of second input wrench
        :return: (geometry_msgs.WrenchStamped)
        """
        newForce = (self.as_array(wrench1.force) * scale1 + self.as_array(wrench2.force) * scale2) * 1/(scale1 + scale2)
        newTorque = (self.as_array(wrench1.torque) * scale1 + self.as_array(wrench2.torque) * scale2) * 1/(scale1 + scale2)
        return self.create_wrench([newForce[0], newForce[1], newForce[2]], [newTorque[0], newTorque[1], newTorque[2]]).wrench

    def update_avg_speed(self):
        """Updates a simple moving average of robot tcp speed in mm/s. A speed is calculated from the difference between a
         previous pose (.1 s in the past) and the current pose; this speed is filtered and stored as self.average_speed.
        """
        curr_time = rospy.get_rostime() - self._start_time
        if(curr_time.to_sec() > rospy.Duration(.5).to_sec()):
            try:
                earlierPosition = self.tf_buffer.lookup_transform("base_link", self.tool_data[self.activeTCP]['transform'].child_frame_id, 
                    rospy.Time.now() - rospy.Duration(.1), rospy.Duration(2.0))
            except (tf2_ros.LookupException, tf2_ros.ConnectivityException, tf2_ros.ExtrapolationException):
                raise
            #Speed Diff: distance moved / time between poses
            positionDiff = self.as_array(self.current_pose.transform.translation) - self.as_array(earlierPosition.transform.translation)
            timeDiff = ((self.current_pose.header.stamp) - (earlierPosition.header.stamp)).to_sec()
            if(timeDiff > 0.0): #Update only if we're using a new pose; also, avoid divide by zero
                speedDiff = positionDiff / timeDiff
                #Moving averate weighted toward old speed; response is independent of rate selected.
                self.average_speed = self.average_speed * (1-10/self._rate_selected) + speedDiff * (10/self._rate_selected)
        else:
            rospy.logwarn_throttle(1.0, "Too early to report past time!" + str(curr_time.to_sec()))
    @staticmethod
    def as_array(vec):
        return np.array([vec.x, vec.y, vec.z])
    
    #See if the force/speed (any vector) is within a 3-d bound. Technically, it is a box, with sqrt(2)*bound okay at diagonals.
    def vectorRegionCompare_symmetrical(self, input, bounds_max):
        """See ``vectorRegionCompare``_. Compares an input to boundaries element-wise. Essentially checks whether a vector
         is within a rectangular region. This version assumes min values to be the negative of max values.
        :param input: (list of floats) x,y,z of a vector to check.
        :param bounds_max: (list of floats) x,y,z max value of each element.
        :return: (bool) Whether the vector falls within the region.
        """

        #initialize a minimum list
        bounds_min = [0,0,0] 
        #Each min value is the negative of the max value
        #Create bounds_min to be the negative of bounds_max. symmetrical, duh....
        bounds_min[0] = bounds_max[0] * -1.0
        bounds_min[1] = bounds_max[1] * -1.0
        bounds_min[2] = bounds_max[2] * -1.0
        return self.vectorRegionCompare(input, bounds_max, bounds_min)
    
    # bounds_max and bounds_min let you set a range for each dimension. 
    #This just compares if you are in the cube described above. 
    def vectorRegionCompare(self, input, bounds_max, bounds_min):
        """.. vectorRegionCompare Compares an input to boundaries element-wise. Essentially checks whether a vector is within a rectangular region.
        :param input: (list of floats) x,y,z of a vector to check.
        :param bounds_max: (list of floats) x,y,z max value of each element.
        :param bounds_min: (list of floats) x,y,z min value of each element.
        :return: (bool) Whether the vector falls within the region. 
        """
        #Simply compares abs. val.s of input's elements to a vector of maximums and returns whether it exceeds
        #if(symmetrical):
        #    bounds_min[0], bounds_min[1], bounds_min[2] = bounds_max[0] * -1, bounds_max[1] * -1, bounds_max[2] * -1
        #TODO - convert to a process of numpy arrays! They process way faster because that library is written in C++
        #Note - actually Numpy's allclose() method may be perfect here.
        if( bounds_max[0] >= input[0] >= bounds_min[0]):
            if( bounds_max[1] >= input[1] >= bounds_min[1]):
                if( bounds_max[2] >= input[2] >= bounds_min[2]):
                    return True
        return False

    #TODO: Make the parameters of function part of the constructor or something...
    def force_cap_check(self):
        """Checks whether any forces or torques are dangerously high. There are two levels of response:
            *Elevated levels of force cause this program to pause for 1s. If forces remain high after pause, 
            the system will enter a freewheeling state
            *Dangerously high forces will kill this program immediately to prevent damage.
        :return: 
        """
        if(not (self.vectorRegionCompare_symmetrical(self.as_array(self.current_wrench.wrench.force), [45, 45, 45])
            and self.vectorRegionCompare_symmetrical(self.as_array(self.current_wrench.wrench.torque), [3.5, 3.5, 3.5]))):
                rospy.logerr("*Very* high force/torque detected! " + str(self.current_wrench.wrench))
                rospy.logerr("Killing program.")
                quit() # kills the program. Since the node is required, it kills the ROS application.
                return False
        if(self.vectorRegionCompare_symmetrical(self.as_array(self.current_wrench.wrench.force), [25, 25, 25])):
            if(self.vectorRegionCompare_symmetrical(self.as_array(self.current_wrench.wrench.torque), [2, 2, 2])):
                return True
        rospy.logerr("High force/torque detected! " + str(self.current_wrench.wrench))
        if(self.highForceWarning):
            self.highForceWarning = False
            return False
        else:   
            rospy.logerr("Sleeping for 1s to damp oscillations...")
            self.highForceWarning = True
            rospy.sleep(1) #Want the system to stop for a second in hopes that it prevents higher forces/torques. May not be helping.
        return True
        
class AssemblyFilters():
    """WIP, not used so far.
    """

    def __init__(self):
        #Simple Moving Average Parameters
        self._buffer_window = self._rate_selected #self._rate_selected = 1/Hz since this variable is the rate of ROS commands
        self._data_buffer = []
        # self._moving_avg_data = np. #Empty to start. make larger than we need since np is contiguous memory. Will ignore NaN values.
        # self._data_buffer = np.empty(self._buffer_window)
        # self.avg_it = 0#iterator for allocating the first window in the moving average calculation
        # self._data_buffer = np.zeros(self._buffer_window)
        # self._moving_avg_data = [] #Empty to start

    def simple_moving_average(self, new_data_point, window=None):
        if window == None:
            window =  self._buffer_window #Unless new input provided, use class member

        #Fill up the first window while returning current value, else calculate moving average using constant window
        if len(self._data_buffer) < window:
            self._data_buffer = np.append(self.data_buffer, new_data_point)
            avg = self.calc_moving_average(self._data_buffer, len(self._data_buffer))
        else:
            self._data_buffer = np.append(self._data_buffer, new_data_point) #append new datapoint to the end
            self.data_buffer = np.delete(self.data_buffer, 0) #pop the first element
            avg = self.calc_moving_average(self._data_buffer, window)
        
        return avg
        
    def calc_moving_average(buffered_data, w): #w is the window
        return np.convolve(buffered_data, np.ones(w), 'valid') / w


if __name__ == '__main__':
    rospy.init_node("demo_assembly_application_compliance")
    
    
