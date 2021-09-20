#!/usr/bin/env python

#UR IP Address is now 175.31.1.137
#Computer has to be 175.31.1.150

# Imports for ros
# from _typeshed import StrPath
from builtins import staticmethod
from operator import truediv
from pickle import STRING
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
from tf2_geometry_msgs.tf2_geometry_msgs import do_transform_pose

import tf2_ros
import tf2_py 
# import tf2
import tf2_geometry_msgs


from threading import Lock

from peg_in_hole_demo.assembly_tools import AssemblyTools

from transitions import Machine

#State names
IDLE_STATE           = 'idle state'
CHECK_FEEDBACK_STATE = 'checking load cell feedback'
APPROACH_STATE       = 'approaching hole surface'
FIND_HOLE_STATE      = 'finding hole'
INSERTING_PEG_STATE  = 'inserting peg'
COMPLETION_STATE     = 'completed insertion'
SAFETY_RETRACT_STATE = 'retracing to safety' 


#Trigger names
CHECK_FEEDBACK_TRIGGER     = 'check loadcell feedback'
APPROACH_SURFACE_TRIGGER   = 'start approach'
FIND_HOLE_TRIGGER          = 'surface found'
INSERT_PEG_TRIGGER         = 'hole found'
ASSEMBLY_COMPLETED_TRIGGER = 'assembly completed'
SAFETY_RETRACTION_TRIGGER  = 'retract to safety'
RESTART_TEST_TRIGGER       = 'restart test'

class testing():
    def __init__(self):
        self._wrench_pub = rospy.Publisher('/cartesian_compliance_controller/target_wrench', WrenchStamped, queue_size=10)


class SpiralSearch(AssemblyTools, Machine):



    def __init__(self):

        # self._wrench_pub    = rospy.Publisher('/cartesian_compliance_controller/target_wrench', WrenchStamped, queue_size=10)
        # self._pose_pub      = rospy.Publisher('cartesian_compliance_controller/target_frame', PoseStamped , queue_size=2)
        # self._target_pub    = rospy.Publisher('target_hole_position', PoseStamped, queue_size=2, latch=True)
        # self._ft_sensor_sub = rospy.Subscriber("/cartesian_compliance_controller/ft_sensor_wrench/", WrenchStamped, self.callback_update_wrench, queue_size=2)



        states = [
            IDLE_STATE,
            CHECK_FEEDBACK_STATE,
            APPROACH_STATE, 
            FIND_HOLE_STATE, 
            INSERTING_PEG_STATE, 
            COMPLETION_STATE, 
            SAFETY_RETRACT_STATE
        ]

        transitions = [
            {'trigger':CHECK_FEEDBACK_TRIGGER    , 'source':IDLE_STATE          , 'dest':CHECK_FEEDBACK_STATE, 'after': 'check_load_cell_feedback'},
            {'trigger':APPROACH_SURFACE_TRIGGER  , 'source':CHECK_FEEDBACK_STATE, 'dest':APPROACH_STATE      , 'after': 'finding_surface'         },
            {'trigger':FIND_HOLE_TRIGGER         , 'source':APPROACH_STATE      , 'dest':FIND_HOLE_STATE     , 'after': 'finding_hole'            },
            {'trigger':INSERT_PEG_TRIGGER        , 'source':FIND_HOLE_STATE     , 'dest':INSERTING_PEG_STATE , 'after': 'inserting_peg'           },
            {'trigger':ASSEMBLY_COMPLETED_TRIGGER, 'source':INSERTING_PEG_STATE , 'dest':COMPLETION_STATE    , 'after': 'completed_insertion'     },

            {'trigger':SAFETY_RETRACTION_TRIGGER , 'source':IDLE_STATE          , 'dest':SAFETY_RETRACT_STATE, 'after': 'safety_retraction'       },
            {'trigger':SAFETY_RETRACTION_TRIGGER , 'source':CHECK_FEEDBACK_STATE, 'dest':SAFETY_RETRACT_STATE, 'after': 'safety_retraction'       },
            {'trigger':SAFETY_RETRACTION_TRIGGER , 'source':APPROACH_STATE      , 'dest':SAFETY_RETRACT_STATE, 'after': 'safety_retraction'       },
            {'trigger':SAFETY_RETRACTION_TRIGGER , 'source':FIND_HOLE_STATE     , 'dest':SAFETY_RETRACT_STATE, 'after': 'safety_retraction'       },
            {'trigger':SAFETY_RETRACTION_TRIGGER , 'source':INSERTING_PEG_STATE , 'dest':SAFETY_RETRACT_STATE, 'after': 'safety_retraction'       },
            {'trigger':SAFETY_RETRACTION_TRIGGER , 'source':COMPLETION_STATE    , 'dest':SAFETY_RETRACT_STATE, 'after': 'safety_retraction'       },

            {'trigger':RESTART_TEST_TRIGGER      , 'source':SAFETY_RETRACT_STATE, 'dest':CHECK_FEEDBACK_STATE, 'after': 'check_load_cell_feedback'}


        ]
        rospy.logwarn_once('MRO IS HERE:::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::')
        print(SpiralSearch.__mro__)
        Machine.__init__(self, states=states, transitions=transitions, initial=IDLE_STATE)
        
        ROS_rate = 100 #setup for sleeping in hz
        start_time = rospy.get_rostime() #for _spiral_search_basic_force_control and spiral_search_basic_compliance_control
        AssemblyTools.__init__(self, ROS_rate, start_time)       

    def _update_commands(self):
        rospy.logerr_once("Preparing to publish pose: " + str(self.pose_vec) + " and wrench: " + str(self.wrench_vec))
        self.publish_pose(self.pose_vec)
        self.publish_wrench(self.wrench_vec)
        self._rate.sleep()
        # self._update_commands()

    def check_load_cell_feedback(self):
        switch_state = False
        #Take an average of static sensor reading to check that it's stable.
        while switch_state == False:

            self.all_states_calc()

            rospy.logwarn_once('In the check_load_cell_feedback. switch_state is:' + str(switch_state) )

            if (self.curr_time_numpy > 2):
                self._bias_wrench = self._average_wrench
                rospy.logerr("Measured bias wrench: " + str(self._bias_wrench))

                if( self.vectorRegionCompare_symmetrical(self.as_array(self._bias_wrench.torque), [1,1,1]) 
                and self.vectorRegionCompare_symmetrical(self.as_array(self._bias_wrench.force), [1.5,1.5,5])):
                    rospy.logerr("Starting linear search.")
                    self.next_trigger, switch_state = self.post_action(APPROACH_SURFACE_TRIGGER) 
                else:
                    rospy.logerr("Starting wrench is dangerously high. Suspending. Try restarting robot if values seem wrong.")
                    self.next_trigger, switch_state = self.post_action(SAFETY_RETRACTION_TRIGGER) 

            self._update_commands()

    def finding_surface(self):
        #seek in Z direction until we stop moving for about 1 second. 
        # Also requires "seeking_force" to be compensated pretty exactly by a static surface.
        #Take an average of static sensor reading to check that it's stable.
        switch_state = False
        while switch_state == False:
            # origTCP = self.activeTCP
            # self.activeTCP = "peg_corner_position"
            self.all_states_calc()

            seeking_force = 5
            self.wrench_vec  = self.get_command_wrench([0,0,seeking_force])
            self.pose_vec = self.linear_search_position([0,0,0]) #doesn't orbit, just drops straight downward

            rospy.logwarn_once('In the finding_surface. switch_state is:' + str(switch_state))
 
            if(not self.force_cap_check()):
                self.next_trigger, switch_state = self.post_action(SAFETY_RETRACTION_TRIGGER) 
                rospy.logerr("Force/torque unsafe; pausing application.")
            elif( self.vectorRegionCompare_symmetrical(self.average_speed, [5/1000,5/1000, 1/1000]) 
                and self.vectorRegionCompare(self.as_array(self.current_wrench.wrench.force), [2.5,2.5,seeking_force*-.75], [-2.5,-2.5,seeking_force*-1.25])):
                self.collision_confidence = self.collision_confidence + 1/self._rate_selected
                rospy.logerr_throttle(.5, "Monitoring for flat surface, confidence = " + str(self.collision_confidence))
                #if((rospy.Time.now()-marked_time).to_sec() > .50): #if we've satisfied this condition for 1 second
                if(self.collision_confidence > .90):
                    #Stopped moving vertically and in contact with something that counters push force
                    rospy.logerr("Flat surface detected! Moving to spiral search!")
                    #Measure flat surface height:
                    self.surface_height = self.current_pose.transform.translation.z
                    self.next_trigger, switch_state = self.post_action(FIND_HOLE_TRIGGER) 
                    self.collision_confidence = 0.01
            else:
                self.collision_confidence = np.max( np.array([self.collision_confidence * 95/self._rate_selected, .001]))
 
            self._update_commands()
            # self.activeTCP = origTCP

    def finding_hole(self):
        #Spiral until we descend 1/3 the specified hole depth (provisional fraction)
        #This triggers the hole position estimate to be updated to limit crazy
        #forces and oscillations. Also reduces spiral size.
        switch_state = False
        while switch_state == False:

            self.all_states_calc()

            seeking_force = 7.0
            self.wrench_vec  = self.get_command_wrench([0,0,seeking_force])
            self.pose_vec = self.spiral_search_basic_compliance_control()
 
            if(not self.force_cap_check()):
                self.next_trigger, switch_state = self.post_action(SAFETY_RETRACTION_TRIGGER) 
                rospy.logerr("Force/torque unsafe; pausing application.")
            elif( self.current_pose.transform.translation.z <= self.surface_height - .0005):
                #If we've descended at least 5mm below the flat surface detected, consider it a hole.
                self.collision_confidence = self.collision_confidence + 1/self._rate_selected
                rospy.logerr_throttle(.5, "Monitoring for hole location, confidence = " + str(self.collision_confidence))
                if(self.collision_confidence > .90):
                        #Descended from surface detection point. Updating hole location estimate.
                        self.x_pos_offset = self.current_pose.transform.translation.x
                        self.y_pos_offset = self.current_pose.transform.translation.y
                        self._amp_limit_cp = 2 * np.pi * 4 #limits to 3 spirals outward before returning to center.
                        #TODO - Make these runtime changes pass as parameters to the "spiral_search_basic_compliance_control" function
                        rospy.logerr_throttle(1.0, "Hole found, peg inserting...")
                        self.next_trigger, switch_state = self.post_action(INSERT_PEG_TRIGGER) 
            else:
                self.collision_confidence = np.max( np.array([self.collision_confidence * 95/self._rate_selected, .01]))
                if(self.current_pose.transform.translation.z >= self.surface_height - self.hole_depth):
                    rospy.logwarn_throttle(.5, "Height is still " + str(self.current_pose.transform.translation.z) 
                        + " whereas we should drop down to " + str(self.surface_height - self.hole_depth) )

            self._update_commands()

    
    def inserting_peg(self):
        #Continue spiraling downward. Outward normal force is used to verify that the peg can't move
        #horizontally. We keep going until vertical speed is very near to zero.
        switch_state = False
        while switch_state == False:

            self.all_states_calc()

            seeking_force = 5.0
            self.wrench_vec  = self.get_command_wrench([0,0,seeking_force])
            self.pose_vec = self.full_compliance_position()
 
            if(not self.force_cap_check()):
                self.next_trigger, switch_state = self.post_action(SAFETY_RETRACTION_TRIGGER) 
                rospy.logerr("Force/torque unsafe; pausing application.")
            elif( self.vectorRegionCompare_symmetrical(self.average_speed, [2.5/1000,2.5/1000,.5/1000]) 
                #and not self.vectorRegionCompare(self.as_array(self.current_wrench.wrench.force), [6,6,80], [-6,-6,-80])
                and self.vectorRegionCompare(self.as_array(self.current_wrench.wrench.force), [1.5,1.5,seeking_force*-.75], [-1.5,-1.5,seeking_force*-1.25])
                and self.current_pose.transform.translation.z <= self.surface_height - self.hole_depth):
                self.collision_confidence = self.collision_confidence + 1/self._rate_selected
                rospy.logerr_throttle(.5, "Monitoring for peg insertion, confidence = " + str(self.collision_confidence))
                #if((rospy.Time.now()-marked_time).to_sec() > .50): #if we've satisfied this condition for 1 second
                if(self.collision_confidence > .90):
                        #Stopped moving vertically and in contact with something that counters push force
                        rospy.logerr_throttle(1.0, "Hole found, peg inserted! Done!")
                        self.next_trigger, switch_state = self.post_action(ASSEMBLY_COMPLETED_TRIGGER) 
            else:
                #rospy.logwarn_throttle(.5, "NOT a flat surface. Time: " + str((rospy.Time.now()-marked_time).to_sec()))
                self.collision_confidence = np.max( np.array([self.collision_confidence * 95/self._rate_selected, .01]))
                if(self.current_pose.transform.translation.z >= self.surface_height - self.hole_depth):
                    rospy.logwarn_throttle(.5, "Height is still " + str(self.current_pose.transform.translation.z) 
                        + " whereas we should drop down to " + str(self.surface_height - self.hole_depth) )
    
            self._update_commands()

    def completed_insertion(self):
        #Inserted properly.
        switch_state = False
        while switch_state == False:

            self.all_states_calc()

            rospy.logwarn_throttle(.50, "Hole found, peg inserted! Done!")
            if(self.current_pose.transform.translation.z > self.restart_height+.07):
                #High enough, won't pull itself upward.
                seeking_force = -2.5
            else:
                #pull upward gently to move out of trouble hopefully.
                seeking_force = -10
            self.force_cap_check()
            self.pose_vec = self.full_compliance_position()

            self._update_commands()

    def safety_retraction(self):
        #Safety passivation; chill and pull out. Actually restarts itself if everything's chill enough.

        switch_state = False
        while switch_state == False:

            self.all_states_calc()

            if(self.current_pose.transform.translation.z > self.restart_height+.05):
                #High enough, won't pull itself upward.
                seeking_force = -3.5
            else:
                #pull upward gently to move out of trouble hopefully.
                seeking_force = -7
            self.wrench_vec  = self.get_command_wrench([0,0,seeking_force])
            self.pose_vec = self.full_compliance_position()

            rospy.logerr_throttle(.5, "Task suspended for safety. Freewheeling until low forces and height reset above .20: " + str(self.current_pose.transform.translation.z))
            if( self.vectorRegionCompare_symmetrical(self.average_speed, [2/1000,2/1000,3/1000]) 
                and self.vectorRegionCompare_symmetrical(self.as_array(self.current_wrench.wrench.force), [2,2,6])
                and self.current_pose.transform.translation.z > self.restart_height):
                self.collision_confidence = self.collision_confidence + .5/self._rate_selected
                rospy.logerr_throttle(.5, "Static. Restarting confidence: " + str( np.round(self.collision_confidence, 2) ) + " out of 1.")
                #if((rospy.Time.now()-marked_time).to_sec() > .50): #if we've satisfied this condition for 1 second
                if(self.collision_confidence > 1):
                        #Restart Search
                        rospy.logerr_throttle(1.0, "Restarting test!")
                        self.next_trigger, switch_state = self.post_action(ASSEMBLY_COMPLETED_TRIGGER) 
            else:
                self.collision_confidence = np.max( np.array([self.collision_confidence * 90/self._rate_selected, .01]))
                if(self.current_pose.transform.translation.z > self.restart_height):
                    rospy.logwarn_throttle(.5, "That's high enough! Let robot stop and come to zero force.")

            self._update_commands()

    #All state callbacks need to calculate this in a while loop
    def all_states_calc(self):
        #All once-per-loop functions
        self.current_pose = self._get_current_pos()
        self.curr_time = rospy.get_rostime() - self._start_time
        self.curr_time_numpy = np.double(self.curr_time.to_sec())
        marked_state = 1; #returns to this state after a soft restart in state 99
        self.wrench_vec  = self.get_command_wrench([0,0,-2])
        self.pose_vec = self.full_compliance_position()
        self.update_avg_speed()
        self.update_average_wrench()
        # self._update_plots()
        rospy.logwarn_throttle(1, "Average wrench in newtons  is " + str(self.as_array(self._average_wrench.force))+ 
             str(self.as_array(self._average_wrench.torque)))
        rospy.logwarn_throttle(1, "Average speed in mm/second is " + str(1000*self.average_speed))

    # def publish_wrench(self, input_vec):
    #     # self.check_controller(self.force_controller)
    #     # forces, torques = self.com_to_tcp(result[:3], result[3:], transform)
    #     # result_wrench = self.create_wrench(result[:3], result[3:])
    #     # result_wrench = self.create_wrench([7,0,0], [0,0,0])
    #     result_wrench = self.create_wrench(input_vec[:3], input_vec[3:])
        
    #     self._wrench_pub.publish(result_wrench)

    def callback_update_wrench(self, data):
        self.current_wrench = data
        rospy.logwarn_once("Callback working! " + str(data))

    # # def publish_pose(self, position, orientation):
    # def publish_pose(self, pose_stamped_vec):
    #     #Takes in vector representations of position vector (x,y,z) and orientation quaternion
    #     # Ensure controller is loaded
    #     # self.check_controller(self.controller_name)

    #     # Create poseStamped msg
    #     pose_stamped = PoseStamped()

    #     # Set the position and orientation
    #     point = Point()
    #     quaternion = Quaternion()

    #     # point.x, point.y, point.z = position
    #     point.x, point.y, point.z = pose_stamped_vec[0][:]
    #     pose_stamped.pose.position = point

    #     quaternion.w, quaternion.x, quaternion.y, quaternion.z  = pose_stamped_vec[1][:]
    #     pose_stamped.pose.orientation = quaternion

    #     # Set header values
    #     pose_stamped.header.stamp = rospy.get_rostime()
    #     pose_stamped.header.frame_id = "base_link"

    #     self._pose_pub.publish(pose_stamped)

    def _algorithm_compliance_control(self):
        # state = 0
        # cycle = 0
        self._average_wrench = self._first_wrench.wrench
        self.collision_confidence = 0
        
        rospy.logwarn_once('BELOW IS THE STATE BEFORE CHECK_FEEDBACK_TRIGGER')
        print(self.state)

        if not rospy.is_shutdown():
            self.trigger(CHECK_FEEDBACK_TRIGGER)

        while not rospy.is_shutdown():
            rospy.logwarn('BELOW IS THE STATE BEING TRANSITIONED FROM:')
            print(self.state)
            self.trigger(self.next_trigger)
            rospy.logwarn('BELOW IS THE STATE BEING TRANSITIONED TO:')
            print(self.state)        
            # self.publish_pose(self.pose_vec)
            # self.publish_wrench(self.wrench_vec)
            # self._rate.sleep()



    def main(self):
        # rospy.init_node("demo_assembly_application_compliance")

        # assembly_application = SpiralSearch()
        # assembly_application._algorithm_force_control()

        #---------------------------------------------COMPLIANCE CONTROL BELOW, FORCE CONTROL ABOVE
        rospy.logwarn_once('MADE IT TO MAIN FUNCTION!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!')
        rospy.sleep(3.5)
        # assembly_application._init_plot()

        self._algorithm_compliance_control()

if __name__ == '__main__':
    
    assembly_application = SpiralSearch()

    assembly_application.main()
    